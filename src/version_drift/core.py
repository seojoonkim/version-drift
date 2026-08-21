"""Safety-first drift detection for bounded local Git checkouts.

VersionDrift classifies repositories before acting and applies only clean,
behind-only fast-forwards. Dirty, untracked, ahead, diverged, detached, and
missing-upstream repositories are reported and preserved.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
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


@dataclass(frozen=True)
class GitResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_git(
    repo: Path,
    args: Sequence[str],
    timeout_s: float = 20.0,
    optional_locks: bool = True,
) -> GitResult:
    env = None
    if not optional_locks:
        env = os.environ.copy()
        env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout_s, check=False, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GitResult(False, stderr=str(exc), returncode=124)
    return GitResult(proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip(), proc.returncode)


def default_roots() -> List[str]:
    env = os.environ.get("VERSION_DRIFT_ROOTS", "").strip()
    if env:
        return [part.strip() for part in env.split(os.pathsep) if part.strip()]
    return [str(Path.cwd().resolve())]


def discover_projects(roots: Iterable[str], max_depth: int = 5) -> List[Path]:
    """Find Git worktrees under explicit roots without following hidden caches."""
    found: List[Path] = []
    seen: set[Path] = set()
    for raw_root in roots:
        if not raw_root:
            continue
        root = Path(raw_root).expanduser().resolve()
        if not root.exists():
            continue
        stack: List[Tuple[Path, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                real = current.resolve()
            except OSError:
                continue
            if real in seen:
                continue
            seen.add(real)
            if (current / ".git").exists():
                found.append(current)
            if depth >= max_depth:
                continue
            try:
                children = sorted(current.iterdir(), key=lambda item: item.name)
            except OSError:
                continue
            for child in reversed(children):
                try:
                    is_dir = child.is_dir()
                except OSError:
                    continue
                if not is_dir or child.name in SKIP_DIR_NAMES:
                    continue
                if child.name.startswith(".") and child.name != ".config":
                    continue
                stack.append((child, depth + 1))
    return sorted(set(found), key=lambda item: str(item))


def _fingerprint(lines: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def inspect_project(
    path: str,
    fetch: bool = False,
    optional_locks: bool = True,
) -> Dict[str, Any]:
    """Return a normalized drift report, optionally suppressing Git index refreshes."""
    repo = Path(path).expanduser().resolve()

    def run_git(args: Sequence[str], timeout_s: float = 20.0) -> GitResult:
        if optional_locks:
            return _run_git(repo, args, timeout_s=timeout_s)
        return _run_git(repo, args, timeout_s=timeout_s, optional_locks=False)
    report: Dict[str, Any] = {
        "schema": "version-drift/1", "checked_at": _now(), "path": str(repo),
        "exists": repo.exists(), "is_git": False, "ok": False,
        "safe_to_update": False, "state": "missing", "reasons": [], "actions": [],
        "working_files_changed": 0,
    }
    if not repo.exists():
        report["reasons"].append("path_missing")
        return report

    top = run_git(["rev-parse", "--show-toplevel"])
    if not top.ok:
        report.update(state="not_git", error=top.stderr)
        report["reasons"].append("not_git")
        return report
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
        return report
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
        return report
    if dirty_lines:
        report["reasons"].append("dirty_worktree")
        report["actions"].append("preserve_or_commit_local_changes_before_sync")
    if not upstream.ok or not upstream.stdout:
        report["reasons"].append("missing_upstream")
        report["actions"].append("set_or_confirm_tracking_branch")
        report["state"] = "protected"
        return report

    counts = run_git(["rev-list", "--left-right", "--count", "HEAD...@{upstream}"])
    if not counts.ok or not counts.stdout:
        report["reasons"].append("ahead_behind_unavailable")
        report["state"] = "protected"
        return report
    parts = counts.stdout.split()
    report["ahead"] = int(parts[0])
    report["behind"] = int(parts[1])
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
    return report


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


def _working_content_snapshot(path: Path) -> Tuple[str, List[str], bool]:
    diff = _run_git(path, ["diff", "--binary", "HEAD", "--"])
    tracked_names = _run_git(path, ["diff", "--name-only", "HEAD", "--"])
    untracked_names = _run_git(path, ["ls-files", "--others", "--exclude-standard"])
    if not diff.ok or not tracked_names.ok or not untracked_names.ok:
        return "", [], False
    names = sorted(set(tracked_names.stdout.splitlines()) | set(untracked_names.stdout.splitlines()))
    digest = hashlib.sha256(diff.stdout.encode("utf-8", errors="surrogateescape"))
    for name in untracked_names.stdout.splitlines():
        digest.update(name.encode("utf-8", errors="surrogateescape"))
        try:
            digest.update((path / name).read_bytes())
        except OSError:
            return "", names, False
    return digest.hexdigest(), names, True


def _repository_snapshots(paths: Iterable[Path]) -> Dict[str, Dict[str, Any]]:
    snapshots: Dict[str, Dict[str, Any]] = {}
    for raw_path in paths:
        path = Path(raw_path).resolve()
        head = _run_git(path, ["rev-parse", "HEAD"])
        status = _run_git(path, ["status", "--porcelain=v1", "-uall"])
        content_fingerprint, content_paths, content_ok = _working_content_snapshot(path)
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
    payload = dict(report)
    payload["event"] = event
    path = _event_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return payload


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


def scan_projects(roots: Iterable[str], base_dir: Optional[str] = None, fetch: bool = False, max_depth: int = 5) -> Dict[str, Any]:
    projects = discover_projects(roots, max_depth=max_depth)
    before = _repository_snapshots(projects)
    reports = [inspect_project(str(path), fetch=fetch) for path in projects]
    for report in reports:
        record_event(report, base_dir=base_dir, event="scan")
    result = {"summary": summarize(reports), "projects": reports}
    return _attach_change_measurement(result, before, _repository_snapshots(projects))


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
    before = inspect_project(path, fetch=fetch)
    result: Dict[str, Any] = {"before": before, "applied": False, "after": None}
    if _sync_blocked(before):
        result.update(blocked=True, reason="only_clean_fast_forward_sync_is_allowed")
        record_event(before, base_dir=base_dir, event="sync_blocked")
        return result
    if not apply:
        result.update(blocked=False, reason="dry_run_fast_forward_available")
        record_event(before, base_dir=base_dir, event="sync_dry_run")
        return result

    current = inspect_project(path, fetch=False)
    if not _same_apply_snapshot(before, current):
        result.update(blocked=True, reason="state_changed_before_apply", after=current)
        record_event(current, base_dir=base_dir, event="sync_state_changed")
        return result

    repo = Path(str(current["path"]))
    pull = _run_git(repo, ["pull", "--ff-only"], timeout_s=120.0)
    after = inspect_project(str(repo), fetch=False)
    result.update(
        blocked=not pull.ok, after=after,
        pull={"ok": pull.ok, "stdout": pull.stdout[-1000:], "stderr": pull.stderr[-1000:]},
        applied=pull.ok and bool(after.get("ok")),
        reason="fast_forward_applied" if pull.ok and after.get("ok") else "fast_forward_failed",
    )
    record_event(after, base_dir=base_dir, event="sync_applied" if result["applied"] else "sync_failed")
    return result


def sync_projects(roots: Iterable[str], base_dir: Optional[str] = None, apply: bool = False, fetch: bool = True, max_depth: int = 5) -> Dict[str, Any]:
    """Preview or apply safe fast-forwards across repositories under roots."""
    projects = discover_projects(roots, max_depth=max_depth)
    before_snapshots = _repository_snapshots(projects)
    results = [
        sync_project(str(repo), base_dir=base_dir, apply=apply, fetch=fetch)
        for repo in projects
    ]
    summary = {
        "total": len(results),
        "safe": sum(item["before"].get("state") == "behind_clean" for item in results),
        "protected": sum(item["before"].get("state") not in {"synced", "behind_clean"} for item in results),
        "synced": sum(item["before"].get("state") == "synced" for item in results),
        "applied": sum(bool(item.get("applied")) for item in results),
        "failed": sum(item.get("reason") in {"fast_forward_failed", "state_changed_before_apply"} for item in results),
        "working_files_changed": 0,
    }
    result = {"summary": summary, "projects": results}
    return _attach_change_measurement(result, before_snapshots, _repository_snapshots(projects))


__all__ = [
    "GitResult", "default_roots", "discover_projects", "inspect_project", "is_safe_fast_forward",
    "record_event", "scan_projects", "summarize", "sync_project", "sync_projects",
]
