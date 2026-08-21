"""Daily change inbox for VersionDrift repository states."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .atomicio import atomic_write_text
from .core import _event_path, scan_projects

SNAPSHOT_SCHEMA = "version-drift/inbox/1"
_FIELDS = ("state", "ahead", "behind", "reasons", "head", "upstream", "status_fingerprint")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def snapshot_path(base_dir: Optional[str] = None) -> Path:
    return _event_path(base_dir).parent / "inbox_snapshot.json"


def _record(report: Dict[str, Any]) -> Dict[str, Any]:
    return {field: report.get(field) for field in _FIELDS}


def _actionable(reports: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        str(Path(report["path"]).resolve()): _record(report)
        for report in reports
        if report.get("state") != "synced"
    }


def load_snapshot(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid inbox snapshot: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid inbox snapshot: top level must be an object")
    repos = payload.get("repos")
    if payload.get("schema") != SNAPSHOT_SCHEMA or not isinstance(repos, dict):
        raise ValueError("invalid inbox snapshot: unsupported schema")
    if not isinstance(payload.get("created_at"), str) or not isinstance(payload.get("roots"), list):
        raise ValueError("invalid inbox snapshot: missing metadata")
    if not all(isinstance(root, str) for root in payload["roots"]):
        raise ValueError("invalid inbox snapshot: roots must be strings")
    for repo, record in repos.items():
        if not isinstance(repo, str) or not isinstance(record, dict):
            raise ValueError("invalid inbox snapshot: malformed repository record")
        if set(record) != set(_FIELDS):
            raise ValueError("invalid inbox snapshot: malformed repository fields")
    return payload


def build_inbox(
    roots: Iterable[str], base_dir: Optional[str] = None, fetch: bool = False, max_depth: int = 5
) -> Dict[str, Any]:
    resolved_roots = [str(Path(root).expanduser().resolve()) for root in roots]
    path = snapshot_path(base_dir)
    previous = load_snapshot(path)
    scan = scan_projects(resolved_roots, base_dir=base_dir, fetch=fetch, max_depth=max_depth)
    observed = {
        str(Path(report["path"]).resolve()): report
        for report in scan["projects"]
    }
    current = _actionable(scan["projects"])
    prior = previous["repos"] if previous else {}

    new = [
        {"repo": repo, "current": current[repo]}
        for repo in sorted(current.keys() - prior.keys())
    ]
    changed = [
        {"repo": repo, "previous": prior[repo], "current": current[repo]}
        for repo in sorted(current.keys() & prior.keys())
        if current[repo] != prior[repo]
    ]
    resolved_repos = {
        repo
        for repo in prior.keys() & observed.keys()
        if observed[repo].get("state") == "synced"
    }
    resolved = [
        {"repo": repo, "previous": prior[repo]}
        for repo in sorted(resolved_repos)
    ]
    preserved = {
        repo: prior[repo]
        for repo in prior.keys() - observed.keys()
    }
    generated_at = _now()
    result = {
        "schema": SNAPSHOT_SCHEMA,
        "generated_at": generated_at,
        "previous_snapshot_at": previous.get("created_at") if previous else None,
        "first_run": previous is None,
        "new": new,
        "changed": changed,
        "resolved": resolved,
        "counts": {"new": len(new), "changed": len(changed), "resolved": len(resolved)},
        "scan_summary": scan["summary"],
    }
    snapshot = {
        "schema": SNAPSHOT_SCHEMA,
        "created_at": generated_at,
        "roots": resolved_roots,
        "repos": {**preserved, **current},
    }
    atomic_write_text(path, json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return result
