"""Safety-first drift detection for bounded local Git checkouts.

VersionDrift classifies repositories before acting and applies only clean,
behind-only fast-forwards. Dirty, untracked, ahead, diverged, detached, and
missing-upstream repositories are reported and preserved.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SKIP_DIR_NAMES = {
    ".cache", ".git", ".hg", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".svn", ".tox", ".venv", "__pycache__", "build", "dist", "node_modules",
    "site-packages", "vendor",
}
_EVENT_STORE = ".version-drift/events.jsonl"
HISTORY_SCHEMA = "version-drift/history/1"
PLAN_SCHEMA = "version-drift/plan/1"
SCAN_SCHEMA = "version-drift/scan/1"
SYNC_SCHEMA = "version-drift/sync/1"
PLAN_AUTHORIZATION = (
    "This plan grants no authorization; apply independently reinspects every repository."
)


def validate_max_depth(max_depth: int) -> int:
    if max_depth < 0:
        raise ValueError("max depth must be non-negative")
    return max_depth


@dataclass(frozen=True)
class GitResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


@dataclass(frozen=True)
class _DiscoveryResult:
    projects: List[Path]
    failures: List[Path]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_git(
    repo: Path,
    args: Sequence[str],
    timeout_s: float = 20.0,
    optional_locks: bool = False,
) -> GitResult:
    env = None
    if not optional_locks:
        env = os.environ.copy()
        env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout_s, check=False, env=env, errors="surrogateescape",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GitResult(False, stderr=str(exc), returncode=124)
    return GitResult(proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip(), proc.returncode)


def default_roots() -> List[str]:
    env = os.environ.get("VERSION_DRIFT_ROOTS", "").strip()
    if env:
        return [part.strip() for part in env.split(os.pathsep) if part.strip()]
    return [str(Path.cwd().resolve())]


def _path_has_symlink_component(path: Path) -> bool:
    """Return whether an existing component of path is a symbolic link."""
    absolute = path if path.is_absolute() else Path.cwd() / path
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except FileNotFoundError:
            return False
        except OSError:
            return True
    return False


def _discover_projects_detailed(roots: Iterable[str], max_depth: int = 5) -> _DiscoveryResult:
    """Discover worktrees and retain traversal failures as local scope facts."""
    validate_max_depth(max_depth)
    found: List[Path] = []
    failures: List[Path] = []
    seen: set[Path] = set()
    for raw_root in roots:
        if not raw_root:
            continue
        candidate = Path(raw_root).expanduser()
        if _path_has_symlink_component(candidate):
            continue
        try:
            root = candidate.resolve()
            exists = root.exists()
        except OSError:
            failures.append(candidate.absolute())
            continue
        if not exists:
            continue
        stack: List[Tuple[Path, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                real = current.resolve()
                is_symlink = current.is_symlink()
            except OSError:
                failures.append(current.absolute())
                continue
            if is_symlink:
                continue
            if real in seen:
                continue
            seen.add(real)
            try:
                is_project = (current / ".git").exists()
            except OSError:
                failures.append(current.absolute())
                continue
            if is_project:
                found.append(current)
            if depth >= max_depth:
                continue
            try:
                children = sorted(current.iterdir(), key=lambda item: item.name)
            except OSError:
                failures.append(current.absolute())
                continue
            for child in reversed(children):
                try:
                    is_dir = child.is_dir()
                except OSError:
                    failures.append(child.absolute())
                    continue
                if not is_dir or child.name in SKIP_DIR_NAMES:
                    continue
                if child.name.startswith(".") and child.name != ".config":
                    continue
                stack.append((child, depth + 1))
    return _DiscoveryResult(
        projects=sorted(set(found), key=str),
        failures=sorted(set(failures), key=str),
    )


def discover_projects(roots: Iterable[str], max_depth: int = 5) -> List[Path]:
    """Find Git worktrees under explicit roots without following hidden caches."""
    return _discover_projects_detailed(roots, max_depth=max_depth).projects


_PUBLIC_DISCOVER_PROJECTS = discover_projects


def _scope_projects(
    roots: Sequence[str], max_depth: int, explicit_targets: bool
) -> _DiscoveryResult:
    """Discover worktrees while retaining uninspectable scopes fail-closed."""
    if discover_projects is _PUBLIC_DISCOVER_PROJECTS:
        discovery = _discover_projects_detailed(roots, max_depth=max_depth)
        projects = discovery.projects
        failures = discovery.failures
    else:
        projects = discover_projects(roots, max_depth=max_depth)
        failures = []
    additions: List[Path] = []
    for raw_root in roots:
        if not raw_root:
            continue
        candidate = Path(raw_root).expanduser()
        if _path_has_symlink_component(candidate):
            failures.append(candidate.absolute())
            continue
        try:
            root = candidate.resolve()
            if not root.exists():
                additions.append(root)
                continue
            next(root.iterdir(), None)
            represented = any(project == root or root in project.parents for project in projects)
            if represented:
                continue
            # Only probe an otherwise unrepresented explicit target. This retains bare
            # repositories without adding ordinary empty roots to scan/plan results.
            bare = _run_git(root, ["rev-parse", "--is-bare-repository"])
            if bare.ok and bare.stdout == "true":
                additions.append(root)
            elif explicit_targets:
                additions.append(root)
        except OSError:
            additions.append(candidate.absolute())
    project_set = set(projects) | set(additions)
    return _DiscoveryResult(
        projects=sorted(project_set, key=str),
        failures=sorted(set(failures) - project_set, key=str),
    )


def _fingerprint(lines: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _finalize_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """Attach orthogonal relation and eligibility facts to an inspection report."""
    ahead = report.get("ahead")
    behind = report.get("behind")
    if isinstance(ahead, int) and isinstance(behind, int) and ahead >= 0 and behind >= 0:
        if ahead and behind:
            relation = "diverged"
        elif ahead:
            relation = "ahead"
        elif behind:
            relation = "behind"
        else:
            relation = "in_sync"
    else:
        relation = "unknown"

    hardening_reasons = {
        "detached_head", "operation_in_progress", "shallow_repository",
        "linked_worktree", "submodule_checkout", "contains_submodules",
    }
    if "git_metadata_unreadable" in report.get("reasons", []):
        eligibility = "unknown"
    elif hardening_reasons.intersection(report.get("reasons", [])):
        eligibility = "blocked"
    elif report.get("safe_to_update") is True and relation == "behind":
        eligibility = "eligible"
    elif relation == "unknown":
        eligibility = "unknown"
    else:
        eligibility = "blocked"

    reasons = report.setdefault("reasons", [])
    if report.get("state") == "synced" and "in_sync_no_action" not in reasons:
        reasons.append("in_sync_no_action")
    report["relation"] = relation
    report["eligibility"] = eligibility
    report["reason_codes"] = list(reasons)
    return report


def _git_topology_reasons(repo: Path, run_git: Any) -> List[str]:
    """Read fail-closed repository topology metadata without changing Git state."""
    reasons: List[str] = []
    unreadable = False

    symbolic_head = run_git(["symbolic-ref", "-q", "HEAD"])
    if symbolic_head.ok:
        if not symbolic_head.stdout:
            unreadable = True
    elif symbolic_head.returncode == 1:
        reasons.append("detached_head")
    else:
        unreadable = True

    for marker in (
        "MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD",
        "REVERT_HEAD", "BISECT_LOG",
    ):
        marker_result = run_git(["rev-parse", "--git-path", marker])
        if not marker_result.ok or not marker_result.stdout:
            unreadable = True
            continue
        marker_path = Path(marker_result.stdout)
        if not marker_path.is_absolute():
            marker_path = repo / marker_path
        try:
            marker_path.stat()
        except FileNotFoundError:
            pass
        except OSError:
            unreadable = True
        else:
            if "operation_in_progress" not in reasons:
                reasons.append("operation_in_progress")

    shallow = run_git(["rev-parse", "--is-shallow-repository"])
    if not shallow.ok or shallow.stdout not in {"true", "false"}:
        unreadable = True
    elif shallow.stdout == "true":
        reasons.append("shallow_repository")

    git_dir = run_git(["rev-parse", "--git-dir"])
    common_dir = run_git(["rev-parse", "--git-common-dir"])
    if not git_dir.ok or not git_dir.stdout or not common_dir.ok or not common_dir.stdout:
        unreadable = True
    else:
        def normalized(value: str) -> Path:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = repo / candidate
            return candidate.resolve()

        try:
            if normalized(git_dir.stdout) != normalized(common_dir.stdout):
                reasons.append("linked_worktree")
        except OSError:
            unreadable = True

    superproject = run_git(["rev-parse", "--show-superproject-working-tree"])
    if not superproject.ok:
        unreadable = True
    elif superproject.stdout:
        reasons.append("submodule_checkout")

    gitmodules = run_git(["ls-files", "--error-unmatch", "--", ".gitmodules"])
    if gitmodules.ok:
        if gitmodules.stdout == ".gitmodules":
            reasons.append("contains_submodules")
        else:
            unreadable = True
    elif gitmodules.returncode != 1:
        unreadable = True

    gitlinks = run_git(["ls-files", "--stage"])
    if not gitlinks.ok:
        unreadable = True
    elif any(line.startswith("160000 ") for line in gitlinks.stdout.splitlines()):
        if "contains_submodules" not in reasons:
            reasons.append("contains_submodules")

    if unreadable:
        reasons.append("git_metadata_unreadable")
    return reasons


def inspect_project(
    path: str,
    fetch: bool = False,
    optional_locks: bool = False,
) -> Dict[str, Any]:
    """Return a normalized drift report, optionally suppressing Git index refreshes."""
    repo = Path(path).expanduser().resolve()

    def run_git(args: Sequence[str], timeout_s: float = 20.0) -> GitResult:
        if optional_locks:
            return _run_git(repo, args, timeout_s=timeout_s, optional_locks=True)
        return _run_git(repo, args, timeout_s=timeout_s)
    report: Dict[str, Any] = {
        "schema": "version-drift/1", "checked_at": _now(), "path": str(repo),
        "exists": repo.exists(), "is_git": False, "ok": False,
        "safe_to_update": False, "state": "missing", "reasons": [], "actions": [],
        "working_files_changed": 0,
    }
    if not repo.exists():
        report["reasons"].append("path_missing")
        return _finalize_report(report)

    top = run_git(["rev-parse", "--show-toplevel"])
    if not top.ok:
        report.update(state="not_git", error=top.stderr)
        report["reasons"].append("not_git")
        return _finalize_report(report)
    repo = Path(top.stdout).resolve()
    report.update(path=str(repo), is_git=True)

    if fetch:
        fetched = run_git(["fetch", "--prune", "--tags"], timeout_s=60.0)
        report["fetch"] = {"ok": fetched.ok, "stderr": fetched.stderr[-500:]}
        report["remote_data"] = "fetched_now" if fetched.ok else "fetch_failed"
        if not fetched.ok:
            report["reasons"].append("fetch_failed")
    else:
        report["remote_data"] = "local_tracking_refs"

    topology_reasons = _git_topology_reasons(repo, run_git)
    if topology_reasons:
        report["reasons"].extend(topology_reasons)
        report["state"] = "protected"
        return _finalize_report(report)

    branch = run_git(["branch", "--show-current"])
    head = run_git(["rev-parse", "HEAD"])
    upstream = run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    status = run_git(["status", "--porcelain=v1", "-uall"])
    if not status.ok:
        report.update(
            branch=branch.stdout,
            head=head.stdout,
            upstream=upstream.stdout if upstream.ok else "",
            state="protected",
            error=status.stderr,
        )
        report["reasons"].append("worktree_status_unavailable")
        report["actions"].append("restore_git_status_before_sync")
        return _finalize_report(report)
    dirty_lines = status.stdout.splitlines() if status.stdout else []
    remote_name = run_git(["config", "--get", f"branch.{branch.stdout}.remote"]) if branch.stdout else GitResult(False)
    remote_url = run_git(["remote", "get-url", remote_name.stdout]) if remote_name.ok and remote_name.stdout else GitResult(False)
    report.update(
        branch=branch.stdout, head=head.stdout, upstream=upstream.stdout if upstream.ok else "",
        remote_name=remote_name.stdout if remote_name.ok else "",
        remote_url=remote_url.stdout if remote_url.ok else "",
        dirty=bool(dirty_lines), dirty_count=len(dirty_lines),
        dirty_paths=[line[3:] for line in dirty_lines[:50]],
        status_fingerprint=_fingerprint(dirty_lines),
    )

    if not head.ok:
        report["state"] = "invalid_head"
        report["reasons"].append("invalid_head")
        return _finalize_report(report)
    if dirty_lines:
        report["reasons"].append("dirty_worktree")
        report["actions"].append("preserve_or_commit_local_changes_before_sync")
    if not upstream.ok or not upstream.stdout:
        report["reasons"].append("missing_upstream")
        report["actions"].append("set_or_confirm_tracking_branch")
        report["state"] = "protected"
        return _finalize_report(report)

    counts = run_git(["rev-list", "--left-right", "--count", "HEAD...@{upstream}"])
    if not counts.ok or not counts.stdout:
        report["reasons"].append("ahead_behind_unavailable")
        report["state"] = "protected"
        return _finalize_report(report)
    parts = counts.stdout.split()
    if len(parts) != 2:
        report["reasons"].append("ahead_behind_unavailable")
        report["state"] = "protected"
        return _finalize_report(report)
    try:
        ahead, behind = (int(parts[0]), int(parts[1]))
    except (TypeError, ValueError):
        report["reasons"].append("ahead_behind_unavailable")
        report["state"] = "protected"
        return _finalize_report(report)
    if ahead < 0 or behind < 0:
        report["reasons"].append("ahead_behind_unavailable")
        report["state"] = "protected"
        return _finalize_report(report)
    report["ahead"] = ahead
    report["behind"] = behind
    ahead, behind = report["ahead"], report["behind"]

    if "fetch_failed" in report["reasons"]:
        report["state"] = "protected"
        report["safe_to_update"] = False
        report["actions"].append("retry_fetch_before_sync")
    elif dirty_lines:
        report["state"] = "protected"
    elif ahead and behind:
        report["state"] = "diverged"
        report["reasons"].append("diverged_from_upstream")
        report["actions"].append("manual_rebase_or_merge_required")
    elif ahead:
        report["state"] = "ahead"
        report["reasons"].append("local_ahead_of_upstream")
        report["actions"].append("push_or_open_pr")
    elif behind:
        report["state"] = "behind_clean"
        report["reasons"].append("local_behind_upstream")
        report["actions"].append("fast_forward_pull_available")
        report["safe_to_update"] = True
    elif report["reasons"] == ["fetch_failed"]:
        report["state"] = "protected"
    else:
        report["state"] = "synced"
        report["ok"] = True
    return _finalize_report(report)


def _event_path(base_dir: Optional[str]) -> Path:
    if base_dir:
        return Path(base_dir).expanduser() / _EVENT_STORE
    configured = os.environ.get("VERSION_DRIFT_DIR", "").strip()
    if configured:
        return Path(configured).expanduser() / _EVENT_STORE
    if sys.platform == "darwin":
        state_dir = Path.home() / "Library" / "Application Support" / "VersionDrift"
    else:
        configured_state = Path(os.environ.get("XDG_STATE_HOME", "")).expanduser()
        state_root = configured_state if configured_state.is_absolute() else Path.home() / ".local" / "state"
        state_dir = state_root / "version-drift"
    return state_dir / "events.jsonl"


def _apply_lock_path(path: str, base_dir: Optional[str] = None) -> Path:
    """Return the state-local lock path for a canonical repository path."""
    canonical = str(Path(path).expanduser().resolve())
    lock_name = hashlib.sha256(canonical.encode("utf-8")).hexdigest() + ".lock"
    return _event_path(base_dir).parent / "locks" / lock_name


def _acquire_apply_lock(path: str, base_dir: Optional[str] = None) -> Optional[int]:
    """Atomically acquire a repository apply lock, or return None if held."""
    lock_path = _apply_lock_path(path, base_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None
    content = json.dumps({
        "pid": os.getpid(), "created_at": _now(), "path": str(Path(path).resolve()),
    })
    try:
        os.write(descriptor, (content + "\n").encode("utf-8"))
    except BaseException:
        _release_apply_lock(lock_path, descriptor)
        raise
    return descriptor


def _release_apply_lock(lock_path: Path, descriptor: int) -> None:
    """Release only the lock represented by descriptor."""
    try:
        opened = os.fstat(descriptor)
        try:
            current = lock_path.stat()
        except FileNotFoundError:
            current = None
        if current is not None and (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
            lock_path.unlink()
    finally:
        os.close(descriptor)


def _working_content_snapshot(path: Path) -> Tuple[str, List[str], bool]:
    index = _run_git(path, ["rev-parse", "--git-path", "index"])
    if not index.ok or not index.stdout:
        return "", [], False
    index_path = Path(index.stdout)
    if not index_path.is_absolute():
        index_path = path / index_path
    if not index_path.is_file():
        return "", [], False
    file_descriptor, isolated_name = tempfile.mkstemp(prefix="version-drift-index-")
    os.close(file_descriptor)
    isolated_index = Path(isolated_name)
    try:
        shutil.copyfile(index_path, isolated_index)
        env = os.environ.copy()
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["GIT_INDEX_FILE"] = str(isolated_index)

        def isolated_git(args: Sequence[str]) -> GitResult:
            try:
                proc = subprocess.run(
                    ["git", "-C", str(path), *args],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=20.0,
                    check=False,
                    env=env,
                    errors="surrogateescape",
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return GitResult(False, stderr=str(exc), returncode=124)
            return GitResult(
                proc.returncode == 0,
                proc.stdout.strip(),
                proc.stderr.strip(),
                proc.returncode,
            )

        diff = isolated_git(["diff", "--binary", "HEAD", "--"])
        tracked_names = isolated_git(["diff", "--name-only", "HEAD", "--"])
        untracked_names = isolated_git(["ls-files", "--others", "--exclude-standard"])
    finally:
        isolated_index.unlink(missing_ok=True)
    if not diff.ok or not tracked_names.ok or not untracked_names.ok:
        return "", [], False
    names = sorted(set(tracked_names.stdout.splitlines()) | set(untracked_names.stdout.splitlines()))
    digest = hashlib.sha256(diff.stdout.encode("utf-8", errors="surrogateescape"))
    for name in untracked_names.stdout.splitlines():
        digest.update(name.encode("utf-8", errors="surrogateescape"))
        try:
            target = path / name
            info = target.lstat()
            if target.is_symlink():
                digest.update(os.readlink(target).encode("utf-8", errors="surrogateescape"))
            elif stat.S_ISREG(info.st_mode):
                nofollow = getattr(os, "O_NOFOLLOW", None)
                nonblock = getattr(os, "O_NONBLOCK", None)
                if nofollow is None or nonblock is None:
                    return "", names, False
                flags = os.O_RDONLY | nofollow | nonblock
                descriptor = os.open(target, flags)
                try:
                    opened = os.fstat(descriptor)
                    if not stat.S_ISREG(opened.st_mode):
                        return "", names, False
                    while True:
                        chunk = os.read(descriptor, 1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                finally:
                    os.close(descriptor)
            else:
                digest.update(b"<unsupported>")
        except OSError:
            return "", names, False
    return digest.hexdigest(), names, True


def _repository_snapshots(paths: Iterable[Path]) -> Dict[str, Dict[str, Any]]:
    snapshots: Dict[str, Dict[str, Any]] = {}
    for raw_path in paths:
        try:
            path = Path(raw_path).resolve()
            head = _run_git(path, ["rev-parse", "HEAD"])
            status = _run_git(path, ["status", "--porcelain=v1", "-uall"])
            content_fingerprint, content_paths, content_ok = _working_content_snapshot(path)
        except OSError:
            path = Path(raw_path).absolute()
            head = GitResult(False)
            status = GitResult(False)
            content_fingerprint, content_paths, content_ok = "", [], False
        snapshots[str(path)] = {
            "head": head.stdout if head.ok else "",
            "status": status.stdout.splitlines() if status.ok and status.stdout else [],
            "content_fingerprint": content_fingerprint,
            "content_paths": content_paths,
            "valid": head.ok and status.ok and content_ok,
        }
    return snapshots


def _changed_working_paths(
    before: Dict[str, Dict[str, Any]], after: Dict[str, Dict[str, Any]]
) -> List[str]:
    changed: set[str] = set()
    for repo_path, prior in before.items():
        current = after.get(repo_path)
        if not current or not prior.get("valid") or not current.get("valid"):
            continue
        repo = Path(repo_path)
        prior_status = set(prior.get("status", []))
        current_status = set(current.get("status", []))
        for line in prior_status.symmetric_difference(current_status):
            if len(line) >= 4:
                changed.add(str((repo / line[3:]).resolve()))
        if prior.get("content_fingerprint") != current.get("content_fingerprint"):
            content_paths = set(prior.get("content_paths", [])) | set(current.get("content_paths", []))
            changed.update(str((repo / name).resolve()) for name in content_paths)
        old_head = str(prior.get("head", ""))
        new_head = str(current.get("head", ""))
        if old_head and new_head and old_head != new_head:
            diff = _run_git(repo, ["diff", "--name-only", old_head, new_head])
            if diff.ok:
                changed.update(str((repo / name).resolve()) for name in diff.stdout.splitlines() if name)
    return sorted(changed)


def _attach_change_measurement(
    result: Dict[str, Any], before: Dict[str, Dict[str, Any]], after: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    changed = _changed_working_paths(before, after)
    result["summary"]["working_files_changed"] = len(changed)
    result["summary"]["working_files_changed_paths"] = changed
    return result


def record_event(report: Dict[str, Any], base_dir: Optional[str] = None, event: str = "scan") -> Dict[str, Any]:
    def sanitize(value: Any) -> Any:
        if isinstance(value, str):
            return "".join("\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char for char in value)
        if isinstance(value, dict):
            return {sanitize(key): sanitize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        if isinstance(value, tuple):
            return tuple(sanitize(item) for item in value)
        return value

    payload = sanitize(dict(report))
    payload["event"] = sanitize(event)
    path = _event_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    with path.open("ab") as handle:
        handle.write(serialized)
    return payload


_HISTORY_FIELDS = (
    "event", "checked_at", "path", "state", "branch", "head", "upstream",
    "ahead", "behind", "reasons", "actions", "ok", "safe_to_update",
    "remote_data", "working_files_changed",
)


def history(
    paths: Optional[Iterable[str]] = None,
    base_dir: Optional[str] = None,
    limit: int = 0,
    events: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Read the append-only event trail without changing it or invoking Git."""
    if limit < 0:
        raise ValueError("limit must be non-negative")
    source = _event_path(base_dir)
    roots = [Path(path).expanduser().resolve() for path in paths or []]
    types = sorted(set(events or []))
    result: Dict[str, Any] = {
        "schema": HISTORY_SCHEMA,
        "source": str(source),
        "source_exists": source.is_file(),
        "filters": {"paths": [str(root) for root in roots], "events": types, "limit": limit},
        "counts": {"source_lines": 0, "malformed_lines": 0, "matched": 0, "returned": 0},
        "malformed_lines": 0,
        "events": [],
    }
    if not result["source_exists"]:
        return result
    matched: List[Dict[str, Any]] = []
    with source.open("rb") as handle:
        for sequence, raw_line in enumerate(handle, start=1):
            result["counts"]["source_lines"] += 1
            try:
                line = raw_line.decode("utf-8")
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError("event must be an object")
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                result["counts"]["malformed_lines"] += 1
                continue
            item_path = item.get("path")
            resolved = Path(item_path).expanduser().resolve() if isinstance(item_path, str) else None
            if types and item.get("event") not in types:
                continue
            if roots and (
                resolved is None
                or not any(resolved == root or root in resolved.parents for root in roots)
            ):
                continue
            matched.append({
                "sequence": sequence,
                **{field: item.get(field) for field in _HISTORY_FIELDS},
            })
    result["counts"]["matched"] = len(matched)
    newest = list(reversed(matched))
    result["events"] = newest if limit == 0 else newest[:limit]
    result["counts"]["returned"] = len(result["events"])
    result["malformed_lines"] = result["counts"]["malformed_lines"]
    return result


def summarize(reports: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    states: Dict[str, int] = {}
    for report in reports:
        state = str(report.get("state", "unknown"))
        states[state] = states.get(state, 0) + 1
    return {
        "total": len(reports), "states": states,
        "ok": all(bool(report.get("ok")) for report in reports) if reports else True,
        "drifted": [report["path"] for report in reports if not report.get("ok")],
        "safe_to_update": sum(bool(report.get("safe_to_update")) for report in reports),
        "protected": sum(report.get("state") not in {"synced", "behind_clean"} for report in reports),
        "working_files_changed": 0,
    }


_INSPECTION_FAILURE_REASONS = {
    "path_missing", "not_git", "fetch_failed", "git_metadata_unreadable",
    "worktree_status_unavailable", "invalid_head", "ahead_behind_unavailable",
    "scope_inspection_failed", "event_write_failed",
}


def _inspection_failed(report: Dict[str, Any]) -> bool:
    return bool(_INSPECTION_FAILURE_REASONS.intersection(report.get("reasons", [])))


def _inspection_outcome(reports: Sequence[Dict[str, Any]]) -> str:
    failures = sum(_inspection_failed(report) for report in reports)
    if not failures:
        return "complete"
    return "failed" if failures == len(reports) else "partial"


def _io_failure_report(path: str, reason: str = "scope_inspection_failed") -> Dict[str, Any]:
    report = {
        "schema": "version-drift/1", "checked_at": _now(),
        "path": str(Path(path).expanduser().absolute()), "exists": False,
        "is_git": False, "ok": False, "safe_to_update": False,
        "state": "missing", "reasons": [reason], "actions": [],
        "working_files_changed": 0,
    }
    return _finalize_report(report)


def scan_projects(roots: Iterable[str], base_dir: Optional[str] = None, fetch: bool = False, max_depth: int = 5) -> Dict[str, Any]:
    validate_max_depth(max_depth)
    root_list = list(roots)
    scope = _scope_projects(root_list, max_depth, explicit_targets=False)
    projects = scope.projects
    before = _repository_snapshots(projects)
    reports = []
    for path in projects:
        try:
            reports.append(inspect_project(str(path), fetch=fetch))
        except OSError:
            reports.append(_io_failure_report(str(path)))
    reports.extend(_io_failure_report(str(path)) for path in scope.failures)
    reports.sort(key=lambda report: str(report.get("path", "")))
    for report in reports:
        try:
            record_event(report, base_dir=base_dir, event="scan")
        except (OSError, UnicodeError, ValueError, TypeError):
            report["ok"] = False
            if "event_write_failed" not in report["reasons"]:
                report["reasons"].append("event_write_failed")
            report["reason_codes"] = list(report["reasons"])
    result = {
        "schema": SCAN_SCHEMA,
        "outcome": _inspection_outcome(reports),
        "summary": summarize(reports),
        "projects": reports,
    }
    return _attach_change_measurement(result, before, _repository_snapshots(projects))


def plan_projects(
    roots: Iterable[str],
    base_dir: Optional[str] = None,
    fetch: bool = True,
    max_depth: int = 5,
) -> Dict[str, Any]:
    """Record inspection-only fast-forward decisions without authorizing apply."""
    validate_max_depth(max_depth)
    root_list = list(roots)
    scope = _scope_projects(root_list, max_depth, explicit_targets=False)
    projects = scope.projects
    repositories: List[Dict[str, Any]] = []
    for project in projects:
        try:
            report = inspect_project(str(project), fetch=fetch)
        except OSError:
            report = _io_failure_report(str(project))
        eligible = report.get("eligibility") == "eligible"
        entry = {
            "path": report.get("path"),
            "relation": report.get("relation"),
            "eligibility": report.get("eligibility"),
            "reason_codes": list(report.get("reason_codes") or []),
            "planned_action": "fast_forward" if eligible else "none",
            "evidence": {
                key: report.get(key)
                for key in ("head", "upstream", "ahead", "behind", "status_fingerprint")
            },
        }
        repositories.append(entry)
        try:
            record_event(
                {**report, "planned_action": entry["planned_action"]},
                base_dir=base_dir,
                event="decision_recorded",
            )
        except (OSError, UnicodeError, ValueError, TypeError):
            entry["eligibility"] = "unknown"
            entry["planned_action"] = "none"
            entry["reason_codes"].append("event_write_failed")

    for failed_scope in scope.failures:
        report = _io_failure_report(str(failed_scope))
        repositories.append({
            "path": report["path"], "relation": report["relation"],
            "eligibility": report["eligibility"],
            "reason_codes": list(report["reason_codes"]), "planned_action": "none",
            "evidence": {key: report.get(key) for key in (
                "head", "upstream", "ahead", "behind", "status_fingerprint"
            )},
        })
    repositories.sort(key=lambda repository: str(repository.get("path", "")))

    planned = sum(item["planned_action"] == "fast_forward" for item in repositories)
    unknown = sum(item["eligibility"] == "unknown" for item in repositories)
    blocked = len(repositories) - planned - unknown
    inspection_failures = sum(
        bool(_INSPECTION_FAILURE_REASONS.intersection(item["reason_codes"]))
        for item in repositories
    )
    if repositories and inspection_failures == len(repositories):
        outcome = "failed"
    elif inspection_failures:
        outcome = "partial"
    else:
        outcome = "complete"
    return {
        "schema": PLAN_SCHEMA,
        "generated_at": _now(),
        "fetch_performed": bool(fetch),
        "outcome": outcome,
        "summary": {
            "total": len(repositories),
            "planned": planned,
            "blocked": blocked,
            "unknown": unknown,
        },
        "repositories": repositories,
        "authorization": PLAN_AUTHORIZATION,
    }


def is_safe_fast_forward(report: Dict[str, Any]) -> bool:
    """Return whether the normalized report meets the public sync policy."""
    return report.get("state") == "behind_clean" and report.get("safe_to_update") is True


def _same_apply_snapshot(before: Dict[str, Any], current: Dict[str, Any]) -> bool:
    return (
        is_safe_fast_forward(current)
        and current.get("head") == before.get("head")
        and current.get("status_fingerprint") == before.get("status_fingerprint")
        and current.get("upstream") == before.get("upstream")
    )


def _sync_blocked(report: Dict[str, Any]) -> bool:
    return not is_safe_fast_forward(report)


def sync_project(path: str, base_dir: Optional[str] = None, apply: bool = False, fetch: bool = True) -> Dict[str, Any]:
    candidate = Path(path).expanduser()
    if _path_has_symlink_component(candidate):
        return {
            "before": _io_failure_report(path), "applied": False, "after": None,
            "blocked": True, "reason": "scope_inspection_failed",
        }
    try:
        before = inspect_project(path, fetch=fetch)
    except OSError:
        before = _io_failure_report(path)
    result: Dict[str, Any] = {"before": before, "applied": False, "after": None}

    def event(report: Dict[str, Any], name: str) -> bool:
        try:
            record_event(report, base_dir=base_dir, event=name)
        except (OSError, UnicodeError, ValueError, TypeError):
            return False
        return True

    if _sync_blocked(before):
        result.update(blocked=True, reason="only_clean_fast_forward_sync_is_allowed")
        if not event(before, "sync_blocked"):
            result["reason"] = "event_write_failed"
        return result
    if not apply:
        result.update(blocked=False, reason="dry_run_fast_forward_available")
        if not event(before, "sync_dry_run"):
            result.update(blocked=True, reason="event_write_failed")
        return result

    lock_repo = str(before.get("path", path))
    try:
        lock_path = _apply_lock_path(lock_repo, base_dir)
        descriptor = _acquire_apply_lock(lock_repo, base_dir)
    except OSError:
        result.update(blocked=True, reason="apply_lock_io_failed")
        return result
    if descriptor is None:
        result.update(blocked=True, reason="apply_lock_held")
        if not event(before, "sync_blocked"):
            result["reason"] = "event_write_failed"
        return result

    pull_succeeded = False
    try:
        try:
            current = inspect_project(path, fetch=False)
        except OSError:
            result.update(blocked=True, reason="state_inspection_failed")
            return result
        if not _same_apply_snapshot(before, current):
            result.update(blocked=True, reason="state_changed_before_apply", after=current)
            if not event(current, "sync_state_changed"):
                result["reason"] = "event_write_failed"
            return result

        repo = Path(str(current["path"]))
        if not event(current, "apply_started"):
            result.update(blocked=True, reason="event_write_failed")
            return result
        pull = _run_git(repo, ["pull", "--ff-only"], timeout_s=120.0)
        pull_succeeded = pull.ok
        pull_payload = {"ok": pull.ok, "stdout": pull.stdout[-1000:], "stderr": pull.stderr[-1000:]}
        if not pull.ok and not event(current, "apply_failed"):
            result.update(blocked=True, reason="event_write_failed", pull=pull_payload)
            return result
        try:
            after = inspect_project(str(repo), fetch=False)
        except OSError:
            result.update(blocked=True, applied=False, reason="fast_forward_outcome_unknown", pull=pull_payload)
            return result
        verified = pull.ok and after.get("state") == "synced" and bool(after.get("ok"))
        reason = "fast_forward_applied" if verified else (
            "fast_forward_outcome_unknown" if pull.ok else "fast_forward_failed"
        )
        lifecycle = "apply_verified_success" if verified else (
            "apply_outcome_unknown" if pull.ok else None
        )
        if lifecycle and not event(after, lifecycle):
            result.update(blocked=True, after=after, applied=False,
                          reason="fast_forward_outcome_unknown", pull=pull_payload)
            return result
        result.update(blocked=not verified, after=after, pull=pull_payload,
                      applied=verified, reason=reason)
        if not event(after, "sync_applied" if verified else "sync_failed"):
            result.update(blocked=True, applied=False,
                          reason="fast_forward_outcome_unknown" if pull.ok else "event_write_failed")
        return result
    finally:
        try:
            _release_apply_lock(lock_path, descriptor)
        except OSError:
            result.update(blocked=True, applied=False,
                          reason="fast_forward_outcome_unknown" if pull_succeeded else "apply_lock_io_failed")


def sync_projects(roots: Iterable[str], base_dir: Optional[str] = None, apply: bool = False, fetch: bool = True, max_depth: int = 5) -> Dict[str, Any]:
    """Preview or apply safe fast-forwards across repositories under roots."""
    validate_max_depth(max_depth)
    root_list = list(roots)
    scope = _scope_projects(root_list, max_depth, explicit_targets=True)
    projects = scope.projects
    before_snapshots = _repository_snapshots(projects)
    results = [
        sync_project(str(repo), base_dir=base_dir, apply=apply, fetch=fetch)
        for repo in projects
    ]
    results.extend({
        "before": _io_failure_report(str(path)), "applied": False, "after": None,
        "blocked": True, "reason": "scope_inspection_failed",
    } for path in scope.failures)
    results.sort(key=lambda item: str(item["before"].get("path", "")))
    operational_failures = sum(
        item.get("reason") in {
            "fast_forward_failed", "fast_forward_outcome_unknown", "apply_lock_io_failed",
            "event_write_failed", "state_inspection_failed",
        }
        or _inspection_failed(item["before"])
        for item in results
    )
    applied = sum(bool(item.get("applied")) for item in results)
    if not operational_failures:
        outcome = "complete"
    elif applied:
        outcome = "partial"
    else:
        outcome = "failed"
    summary = {
        "total": len(results),
        "safe": sum(item["before"].get("state") == "behind_clean" for item in results),
        "protected": sum(item["before"].get("state") not in {"synced", "behind_clean"} for item in results),
        "synced": sum(item["before"].get("state") == "synced" for item in results),
        "applied": applied,
        "failed": operational_failures,
        "outcome": outcome,
        "working_files_changed": 0,
    }
    result = {"schema": SYNC_SCHEMA, "outcome": outcome, "summary": summary, "projects": results}
    return _attach_change_measurement(result, before_snapshots, _repository_snapshots(projects))


__all__ = [
    "GitResult", "default_roots", "discover_projects", "inspect_project", "is_safe_fast_forward",
    "history", "plan_projects", "record_event", "scan_projects", "summarize", "sync_project",
    "sync_projects",
]
