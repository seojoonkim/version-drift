"""Deterministic, read-only explanations of VersionDrift reports."""
from __future__ import annotations

import shlex
from typing import Any, Dict, Iterable, List

from .core import is_safe_fast_forward

EXPLAIN_SCHEMA = "version-drift/explain/1"

ACTIONS: Dict[str, Dict[str, Any]] = {
    "synced": {
        "why_it_matters": "The local branch matches its upstream tracking branch.",
        "safe_actions": [
            {"description": "No action is needed."},
        ],
    },
    "behind_clean": {
        "why_it_matters": "Upstream has commits that can be applied as a clean fast-forward.",
        "safe_actions": [
            {"description": "Preview and apply the guarded fast-forward with VersionDrift."},
        ],
    },
    "ahead": {
        "why_it_matters": "Local commits are not present on the upstream tracking branch.",
        "safe_actions": [
            {"description": "Review the local commits before publishing them."},
        ],
    },
    "diverged": {
        "why_it_matters": "Local and upstream history both changed, so VersionDrift will not modify the repository.",
        "safe_actions": [
            {"description": "Inspect both histories and choose a reconciliation approach manually."},
        ],
    },
    "dirty_worktree": {
        "why_it_matters": "Uncommitted or untracked work is present and must be preserved.",
        "safe_actions": [
            {"description": "Review the working tree and preserve or commit local work when ready."},
        ],
    },
    "missing_upstream": {
        "why_it_matters": "No upstream tracking branch is configured, so drift cannot be compared safely.",
        "safe_actions": [
            {"description": "Confirm the intended remote and tracking branch in Git."},
        ],
    },
    "fetch_failed": {
        "why_it_matters": "Remote-tracking data could not be refreshed, so the result is protected.",
        "safe_actions": [
            {"description": "Check network and remote access before requesting a refreshed scan."},
        ],
    },
    "protected": {
        "why_it_matters": "The repository state is incomplete or ambiguous, so VersionDrift will not modify it.",
        "safe_actions": [
            {"description": "Review the reported reasons and Git state manually."},
        ],
    },
    "invalid": {
        "why_it_matters": "The path could not be inspected as a valid Git worktree.",
        "safe_actions": [
            {"description": "Verify that the path exists and points to the intended Git worktree."},
        ],
    },
}


def _explanation_key(report: Dict[str, Any]) -> str:
    state = str(report.get("state", ""))
    reasons = set(str(reason) for reason in report.get("reasons", []))
    if state == "protected":
        for reason in ("dirty_worktree", "missing_upstream", "fetch_failed"):
            if reason in reasons:
                return reason
        return "protected"
    if state in {"synced", "behind_clean", "ahead", "diverged"}:
        return state
    return "invalid"


def _safe_actions(key: str, report: Dict[str, Any]) -> List[Dict[str, str]]:
    actions = [dict(action) for action in ACTIONS[key]["safe_actions"]]
    if key == "behind_clean":
        path = shlex.quote(str(report.get("path", "")))
        actions[0]["command"] = "version-drift sync --apply -- " + path
    return actions


def build_explanation(report: Dict[str, Any]) -> Dict[str, Any]:
    """Project an existing inspection report into stable explanatory output."""
    key = _explanation_key(report)
    branch = report.get("branch") or None
    upstream = report.get("upstream") or None
    ahead = report.get("ahead")
    behind = report.get("behind")
    return {
        "path": str(report.get("path", "")),
        "state": str(report.get("state", "unknown")),
        "reasons": list(report.get("reasons", [])),
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead if isinstance(ahead, int) else None,
        "behind": behind if isinstance(behind, int) else None,
        "explanation_key": key,
        "why_it_matters": str(ACTIONS[key]["why_it_matters"]),
        "safe_actions": _safe_actions(key, report),
        "sync_eligible": is_safe_fast_forward(report),
    }


def explain_reports(reports: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Build deterministic output from normalized inspection reports."""
    repositories = sorted(
        (build_explanation(report) for report in reports),
        key=lambda item: item["path"],
    )
    counts: Dict[str, int] = {}
    for item in repositories:
        state = str(item["state"])
        counts[state] = counts.get(state, 0) + 1
    return {
        "schema": EXPLAIN_SCHEMA,
        "repositories": repositories,
        "summary": {
            "total": len(repositories),
            "by_state": dict(sorted(counts.items())),
            "sync_eligible": sum(bool(item["sync_eligible"]) for item in repositories),
        },
    }


__all__ = ["ACTIONS", "EXPLAIN_SCHEMA", "build_explanation", "explain_reports"]
