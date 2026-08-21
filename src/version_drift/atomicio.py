"""Small atomic-file helper for VersionDrift state."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional


def atomic_write_text(path: Path, text: str) -> None:
    """Replace *path* atomically without exposing a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
