"""VersionDrift: standalone Git checkout drift detection and safe sync."""

from .core import (
    default_roots,
    discover_projects,
    history,
    inspect_project,
    plan_projects,
    record_event,
    scan_projects,
    summarize,
    sync_project,
    sync_projects,
)

__all__ = [
    "default_roots",
    "discover_projects",
    "history",
    "inspect_project",
    "plan_projects",
    "record_event",
    "scan_projects",
    "summarize",
    "sync_project",
    "sync_projects",
]
__version__ = "1.1.0"
