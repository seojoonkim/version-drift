"""Persistent root configuration for VersionDrift."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Iterable, List, Optional

from .atomicio import atomic_write_text


def platform_config_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "VersionDrift"
    configured = Path(os.environ.get("XDG_CONFIG_HOME", "")).expanduser()
    root = configured if configured.is_absolute() else Path.home() / ".config"
    return root / "version-drift"


def config_path() -> Path:
    return platform_config_dir() / "config.toml"


def _canonical_roots(roots: Iterable[str], default_cwd: bool = False) -> List[str]:
    values = [str(Path(root).expanduser().resolve()) for root in roots if root]
    if not values and default_cwd:
        values = [str(Path.cwd().resolve())]
    invalid = [root for root in values if not Path(root).is_dir()]
    if invalid:
        raise ValueError("root is not an existing directory: " + ", ".join(invalid))
    return sorted(set(values))


def write_config(roots: Iterable[str]) -> List[str]:
    resolved = _canonical_roots(roots, default_cwd=True)
    lines = ["schema_version = 1", "roots = ["]
    lines.extend(f"  {json.dumps(root)}," for root in resolved)
    lines.extend(["]", ""])
    atomic_write_text(config_path(), "\n".join(lines))
    return resolved


def load_config(path: Optional[Path] = None) -> List[str]:
    target = path or config_path()
    if not target.exists():
        return []
    roots: List[str] = []
    try:
        lines = [line.strip() for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(lines) < 4 or lines[0] != "schema_version = 1" or lines[1] != "roots = [" or lines[-1] != "]":
            raise ValueError("unsupported schema or malformed roots array")
        for line in lines[2:-1]:
            if not line.endswith(","):
                raise ValueError("root entries must end with a comma")
            root = json.loads(line[:-1])
            if not isinstance(root, str) or not root:
                raise ValueError("root entries must be nonempty strings")
            roots.append(root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid VersionDrift config: {exc}") from exc
    if not roots:
        raise ValueError("invalid VersionDrift config: roots must not be empty")
    return _canonical_roots(roots)


def resolve_roots(explicit: Iterable[str]) -> List[str]:
    requested = [root for root in explicit if root]
    if requested:
        return _canonical_roots(requested)
    configured = load_config()
    if configured:
        return configured
    env = os.environ.get("VERSION_DRIFT_ROOTS", "").strip()
    if env:
        return _canonical_roots(part for part in env.split(os.pathsep) if part)
    return [str(Path.cwd().resolve())]
