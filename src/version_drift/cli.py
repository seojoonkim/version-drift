"""Command-line interface for VersionDrift."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Optional, Sequence

from .core import default_roots, inspect_project, record_event, scan_projects, sync_project, sync_projects


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="version-drift",
        description="Find out-of-sync Git repositories without touching local work",
    )
    parser.add_argument("--base-dir", default="", help="Directory for .version-drift/events.jsonl")
    parser.add_argument("--version", action="version", version="%(prog)s 0.2.0")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Run a read-only Git checkup under bounded roots")
    scan.add_argument("roots", nargs="*", help="Roots to scan; defaults to VERSION_DRIFT_ROOTS or cwd")
    scan.add_argument("--root", action="append", default=[], help="Compatibility alias; repeatable")
    scan.add_argument("--max-depth", type=int, default=5)
    scan.add_argument("--fetch", action="store_true", help="Refresh remote-tracking refs before comparison")
    scan.add_argument("--json", action="store_true")
    scan.add_argument("--check", action="store_true", help="Exit 1 when drift is found")

    inspect = sub.add_parser("inspect", help="Inspect one Git repository")
    inspect.add_argument("path")
    inspect.add_argument("--fetch", action="store_true")
    inspect.add_argument("--json", action="store_true")

    sync = sub.add_parser("sync", help="Preview or apply clean fast-forwards under roots")
    sync.add_argument("paths", nargs="+", help="One repository or one or more roots")
    sync.add_argument("--apply", action="store_true")
    sync.add_argument("--no-fetch", action="store_true")
    sync.add_argument("--max-depth", type=int, default=5)
    sync.add_argument("--json", action="store_true")
    return parser


def _display_path(raw_path: str, roots: Sequence[str]) -> str:
    path = Path(raw_path)
    candidates = []
    for raw_root in roots:
        try:
            candidates.append(str(path.relative_to(Path(raw_root).expanduser().resolve())))
        except ValueError:
            continue
    return min(candidates, key=len) if candidates else str(path)


def _print_scan(result: dict[str, Any], roots: Sequence[str]) -> None:
    summary = result["summary"]
    total = summary["total"]
    noun = "repository" if total == 1 else "repositories"
    scope = ", ".join(str(Path(root).expanduser()) for root in roots)
    print(f"VersionDrift scanned {total} {noun} under {scope}")
    print()
    states = summary["states"]
    rows = [
        ("✓", states.get("synced", 0), "in sync"),
        ("↓", states.get("behind_clean", 0), "safe to update"),
        ("!", states.get("protected", 0), "local work protected"),
        ("↑", states.get("ahead", 0), "ahead of upstream"),
        ("↕", states.get("diverged", 0), "diverged"),
        ("?", states.get("invalid_head", 0) + states.get("not_git", 0) + states.get("missing", 0), "could not inspect"),
    ]
    for marker, count, label in rows:
        if count:
            print(f"  {marker} {count:>2}  {label}")

    safe = [report for report in result["projects"] if report.get("state") == "behind_clean"]
    protected = [report for report in result["projects"] if report.get("state") not in {"synced", "behind_clean"}]
    if safe:
        print("\nSafe to update")
        for report in safe[:10]:
            print(f"  {_display_path(report['path'], roots):<32} {report.get('behind', 0)} commits behind")
    if protected:
        print("\nProtected. VersionDrift will not touch these")
        for report in protected[:10]:
            reason = ", ".join(report.get("reasons") or [report.get("state", "unknown")])
            print(f"  {_display_path(report['path'], roots):<32} {reason}")
    print("\nWorking files changed: 0")
    print("Remote data: refreshed now" if any(report.get("remote_data") == "fetched_now" for report in result["projects"]) else "Remote data: local tracking refs")


def _print_sync(result: dict[str, Any], apply: bool) -> None:
    summary = result["summary"]
    if apply:
        print(f"Applied {summary['applied']} safe fast-forward(s).")
    else:
        print(f"{summary['safe']} repositories can be fast-forwarded.")
        print(f"{summary['protected']} are protected because they contain local work or ambiguous history.")
        print("Nothing was changed. Add --apply to update the safe repositories.")
    print("Working files changed: 0")


def _resolved_scan_roots(args: argparse.Namespace) -> list[str]:
    roots = [*(args.roots or []), *(args.root or [])]
    return roots or default_roots() or [os.getcwd()]


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    base_dir = args.base_dir or None
    if args.command == "scan":
        roots = _resolved_scan_roots(args)
        result = scan_projects(roots, base_dir=base_dir, fetch=args.fetch, max_depth=args.max_depth)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            _print_scan(result, roots)
        return 1 if args.check and not result["summary"]["ok"] else 0
    if args.command == "inspect":
        report = inspect_project(args.path, fetch=args.fetch)
        record_event(report, base_dir=base_dir, event="inspect")
        print(json.dumps(report, ensure_ascii=False, sort_keys=True) if args.json else report)
        return 0 if report.get("ok") else 1

    paths = args.paths
    if len(paths) == 1 and (Path(paths[0]).expanduser() / ".git").exists():
        result = sync_project(paths[0], base_dir=base_dir, apply=args.apply, fetch=not args.no_fetch)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else result)
        return 0 if result.get("applied") or (not args.apply and not result.get("blocked")) else 1
    result = sync_projects(paths, base_dir=base_dir, apply=args.apply, fetch=not args.no_fetch, max_depth=args.max_depth)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        _print_sync(result, args.apply)
    return 2 if result["summary"]["failed"] else 0


def cmd(args: argparse.Namespace) -> int:
    """Compatibility bridge for MemKraft's existing dispatcher."""
    argv: list[str] = []
    if getattr(args, "base_dir", ""):
        argv.extend(["--base-dir", args.base_dir])
    argv.append(args.project_sync_command)
    if args.project_sync_command == "scan":
        argv.extend(getattr(args, "root", []) or [])
        argv.extend(["--max-depth", str(args.max_depth)])
        if getattr(args, "fetch", False):
            argv.append("--fetch")
        if getattr(args, "json", False):
            argv.append("--json")
    elif args.project_sync_command == "inspect":
        argv.append(args.path)
        if getattr(args, "fetch", False):
            argv.append("--fetch")
        if getattr(args, "json", False):
            argv.append("--json")
    else:
        argv.append(args.path)
        if getattr(args, "apply", False):
            argv.append("--apply")
        if getattr(args, "no_fetch", False):
            argv.append("--no-fetch")
        if getattr(args, "json", False):
            argv.append("--json")
    return main(argv)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "cmd", "main"]
