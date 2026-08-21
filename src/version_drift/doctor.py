"""Read-only installation and state diagnostics for VersionDrift."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import config
from .core import _event_path
from .inbox import load_snapshot, snapshot_path

DOCTOR_SCHEMA = "version-drift/doctor/1"
_MINIMUM_PYTHON = (3, 9)


def _check_python() -> Dict[str, Any]:
    current = tuple(sys.version_info[:3])
    supported = current[:2] >= _MINIMUM_PYTHON
    version = ".".join(str(part) for part in current)
    detail = f"Python {version}; minimum supported is 3.9"
    return {"name": "python_runtime", "ok": supported, "detail": detail}


def _check_git() -> Dict[str, Any]:
    proc = subprocess.run(
        ["git", "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10.0,
        check=False,
    )
    version = proc.stdout.strip()
    ok = proc.returncode == 0 and bool(version)
    detail = version if ok else (proc.stderr.strip() or f"git --version exited {proc.returncode}")
    return {"name": "git_executable", "ok": ok, "detail": detail}


def _check_config() -> Dict[str, Any]:
    path = config.config_path()
    if not path.exists():
        return {"name": "config", "ok": True, "detail": f"Not configured; {path} does not exist"}
    roots = config.load_config(path)
    return {
        "name": "config",
        "ok": True,
        "detail": f"Valid and readable: {path}",
        "counts": {"roots": len(roots)},
    }


def _check_events(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {
            "name": "event_history",
            "ok": True,
            "detail": f"No event history yet; {path} does not exist",
            "counts": {"malformed_lines": 0, "source_lines": 0},
        }
    source_lines = 0
    malformed_lines = 0
    with path.open("rb") as handle:
        for raw_line in handle:
            source_lines += 1
            try:
                item = json.loads(raw_line.decode("utf-8"))
                if not isinstance(item, dict):
                    raise ValueError("event must be an object")
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                malformed_lines += 1
    ok = malformed_lines == 0
    detail = (
        f"Readable: {source_lines} event line(s), no malformed lines"
        if ok
        else f"Found {malformed_lines} malformed event line(s); no repairs performed"
    )
    return {
        "name": "event_history",
        "ok": ok,
        "detail": detail,
        "counts": {"malformed_lines": malformed_lines, "source_lines": source_lines},
    }


def _check_snapshot(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"name": "inbox_snapshot", "ok": True, "detail": f"No snapshot yet; {path} does not exist"}
    payload = load_snapshot(path)
    repos = payload["repos"] if payload is not None else {}
    return {
        "name": "inbox_snapshot",
        "ok": True,
        "detail": f"Valid and readable: {path}",
        "counts": {"repositories": len(repos)},
    }


def _check_locks(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {
            "name": "apply_locks",
            "ok": True,
            "detail": f"No lock directory yet; {path} does not exist",
            "counts": {"locks": 0},
            "data": [],
        }
    if not path.is_dir():
        return {"name": "apply_locks", "ok": False, "detail": f"Lock path is not a directory: {path}"}
    locks = sorted(item.name for item in path.iterdir() if item.name.endswith(".lock"))
    if locks:
        detail = f"Found {len(locks)} apply lock(s), stale-or-active-unknown; none removed"
    else:
        detail = "No apply locks found"
    return {
        "name": "apply_locks",
        "ok": not locks,
        "detail": detail,
        "counts": {"locks": len(locks)},
        "data": locks,
    }


def _check_state_directory(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {
            "name": "state_directory",
            "ok": True,
            "detail": f"Not yet created: {path}",
            "data": {"exists": False, "writable": None},
        }
    if not path.is_dir():
        return {
            "name": "state_directory",
            "ok": False,
            "detail": f"State path exists but is not a directory: {path}",
            "data": {"exists": True, "writable": False},
        }
    writable = os.access(str(path), os.W_OK)
    return {
        "name": "state_directory",
        "ok": writable,
        "detail": f"Exists; {'writable' if writable else 'not writable'}: {path}",
        "data": {"exists": True, "writable": writable},
    }


def _safe_check(name: str, check: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    try:
        return check()
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"name": name, "ok": False, "detail": str(exc)}


def run_doctor(base_dir: Optional[str] = None) -> Dict[str, Any]:
    """Run stable, read-only checks without creating or repairing state."""
    event_path = _event_path(base_dir)
    state_dir = event_path.parent
    checks = [
        _safe_check("python_runtime", _check_python),
        _safe_check("git_executable", _check_git),
        _safe_check("config", _check_config),
        _safe_check("event_history", lambda: _check_events(event_path)),
        _safe_check("inbox_snapshot", lambda: _check_snapshot(snapshot_path(base_dir))),
        _safe_check("apply_locks", lambda: _check_locks(state_dir / "locks")),
        _safe_check("state_directory", lambda: _check_state_directory(state_dir)),
    ]
    return {"schema": DOCTOR_SCHEMA, "ok": all(check["ok"] for check in checks), "checks": checks}


__all__ = ["DOCTOR_SCHEMA", "run_doctor"]
