"""Command-line interface for VersionDrift."""
from __future__ import annotations

import argparse
import json
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, Optional, Sequence

from . import __version__
from .config import config_path, resolve_roots, write_config
from .core import (
    discover_projects,
    history,
    inspect_project,
    plan_projects,
    record_event,
    scan_projects,
    sync_projects,
)
from .doctor import run_doctor
from .explain import explain_reports
from .inbox import build_inbox


def _package_version() -> str:
    try:
        return metadata.version("version-drift")
    except metadata.PackageNotFoundError:
        return __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="version-drift",
        description="Find out-of-sync Git repositories without touching local work",
    )
    parser.add_argument("--base-dir", default="", help="Directory for .version-drift/events.jsonl")
    parser.add_argument("--version", action="version", version=f"%(prog)s {_package_version()}")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Run a read-only Git checkup under bounded roots")
    scan.add_argument("roots", nargs="*", help="Roots to scan; defaults to VERSION_DRIFT_ROOTS or cwd")
    scan.add_argument("--root", action="append", default=[], help="Compatibility alias; repeatable")
    scan.add_argument("--max-depth", type=int, default=5)
    scan.add_argument("--fetch", action="store_true", help="Refresh remote-tracking refs before comparison")
    scan.add_argument("--json", action="store_true")
    scan.add_argument("--check", action="store_true", help="Exit 1 when drift is found")

    init = sub.add_parser("init", help="Save default roots without scanning repositories")
    init.add_argument("roots", nargs="*", help="Existing directory roots; defaults to cwd")
    init.add_argument("--json", action="store_true")

    inbox = sub.add_parser("inbox", help="Show repository changes since the previous inbox check")
    inbox.add_argument("roots", nargs="*", help="Optional roots; defaults to saved configuration")
    inbox.add_argument("--max-depth", type=int, default=5)
    inbox.add_argument("--fetch", action="store_true")
    inbox.add_argument("--json", action="store_true")

    explain = sub.add_parser("explain", help="Explain repository states and safe next actions")
    explain.add_argument("paths", nargs="*", help="Repositories to explain; defaults to configured roots")
    explain.add_argument("--max-depth", type=int, default=5)
    explain.add_argument("--json", action="store_true")

    history_parser = sub.add_parser("history", help="Read the local decision history")
    history_parser.add_argument("paths", nargs="*")
    history_parser.add_argument("--limit", type=int, default=0)
    history_parser.add_argument("--event", action="append", default=[])
    history_parser.add_argument("--json", action="store_true")

    doctor = sub.add_parser("doctor", help="Check the runtime and local VersionDrift state")
    doctor.add_argument("--json", action="store_true")

    inspect = sub.add_parser("inspect", help="Inspect one Git repository")
    inspect.add_argument("path")
    inspect.add_argument("--fetch", action="store_true")
    inspect.add_argument("--json", action="store_true")

    sync = sub.add_parser("sync", help="Preview or apply clean fast-forwards under roots")
    sync.add_argument("paths", nargs="+", help="One repository or one or more roots")
    mode = sync.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--plan", action="store_true")
    fetching = sync.add_mutually_exclusive_group()
    fetching.add_argument("--fetch", dest="fetch", action="store_true")
    fetching.add_argument("--no-fetch", dest="fetch", action="store_false")
    sync.set_defaults(fetch=True)
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
    print(f"\nWorking files changed: {summary['working_files_changed']}")
    print("Remote data: refreshed now" if any(report.get("remote_data") == "fetched_now" for report in result["projects"]) else "Remote data: local tracking refs")


def _print_sync(result: dict[str, Any], apply: bool) -> None:
    summary = result["summary"]
    if apply:
        print(f"Applied {summary['applied']} safe fast-forward(s).")
    else:
        print(f"{summary['safe']} repositories can be fast-forwarded.")
        print(f"{summary['protected']} are protected because they contain local work or ambiguous history.")
        print("Nothing was changed. Add --apply to update the safe repositories.")
    print(f"Working files changed: {summary['working_files_changed']}")


def _print_plan(result: dict[str, Any]) -> None:
    summary = result["summary"]
    print(f"Planned fast-forwards: {summary['planned']}")
    print(f"Blocked repositories: {summary['blocked']}")
    if summary["unknown"]:
        print(f"Unknown repositories: {summary['unknown']}")
    for repository in result["repositories"]:
        reasons = ", ".join(repository["reason_codes"]) or "no reason recorded"
        print(f"  {repository['path']}: {repository['planned_action']} ({reasons})")
    print(result["authorization"])
    print("Nothing was changed.")


def _resolved_scan_roots(args: argparse.Namespace) -> list[str]:
    roots = [*(args.roots or []), *(args.root or [])]
    return resolve_roots(roots)


def _print_inbox(result: dict[str, Any]) -> None:
    counts = result["counts"]
    total = sum(counts.values())
    print(f"VersionDrift inbox: {total} change{'s' if total != 1 else ''}")
    labels = (("New", "new"), ("Changed", "changed"), ("Resolved", "resolved"))
    for label, key in labels:
        if not result[key]:
            continue
        print(f"\n{label}")
        for item in result[key]:
            record = item.get("current") or item.get("previous") or {}
            print(f"  {item['repo']}  {record.get('state', 'unknown')}")
    if not total:
        print("No repository state changes since the previous checkup.")
    print(f"Working files changed: {result['scan_summary']['working_files_changed']}")


def _print_explain(result: dict[str, Any]) -> None:
    summary = result["summary"]
    total = summary["total"]
    noun = "repository" if total == 1 else "repositories"
    print(f"VersionDrift explain: {total} {noun}")
    for item in result["repositories"]:
        detail = item["state"]
        if item.get("behind"):
            detail += f" ({item['behind']} commits behind)"
        elif item.get("ahead"):
            detail += f" ({item['ahead']} commits ahead)"
        print(f"\n{item['path']}\n  State: {detail}")
        print(f"  Why: {item['why_it_matters']}")
        for action in item["safe_actions"]:
            print(f"  Next: {action['description']}")
            if action.get("command"):
                print(f"        {action['command']}")
    print("\nNothing was changed.")


def _print_history(result: dict[str, Any]) -> None:
    print(f"VersionDrift history: {result['counts']['returned']} event(s)")
    for item in result["events"]:
        print(f"  {item['sequence']}. {item.get('event')}: {item.get('path')}")
    malformed = result["counts"]["malformed_lines"]
    if malformed:
        noun = "line" if malformed == 1 else "lines"
        print(f"Skipped {malformed} malformed event {noun}.")
    print("Nothing was changed.")


def _print_doctor(result: dict[str, Any]) -> None:
    print(f"VersionDrift doctor: {'ok' if result['ok'] else 'issues found'}")
    for check in result["checks"]:
        print(f"  {'✓' if check['ok'] else '!'} {check['name']}: {check['detail']}")
    print("Nothing was changed.")


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    base_dir = args.base_dir or None
    if hasattr(args, "max_depth") and args.max_depth < 0:
        print(f"version-drift {args.command}: max depth must be non-negative", file=sys.stderr)
        return 2
    if args.command == "doctor":
        result = run_doctor(base_dir)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            _print_doctor(result)
        return 0 if result["ok"] else 1
    if args.command == "init":
        try:
            roots = write_config(args.roots)
        except (OSError, ValueError) as exc:
            print(f"version-drift init: {exc}", file=sys.stderr)
            return 2
        result = {"schema": "version-drift/config/1", "path": str(config_path()), "roots": roots}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else f"Saved {len(roots)} root(s) to {config_path()}")
        return 0
    if args.command == "scan":
        try:
            roots = _resolved_scan_roots(args)
        except ValueError as exc:
            print(f"version-drift scan: {exc}", file=sys.stderr)
            return 2
        result = scan_projects(roots, base_dir=base_dir, fetch=args.fetch, max_depth=args.max_depth)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            _print_scan(result, roots)
        if result["outcome"] in {"partial", "failed"}:
            return 3
        return 1 if args.check and not result["summary"]["ok"] else 0
    if args.command == "inbox":
        try:
            roots = resolve_roots(args.roots)
            result = build_inbox(roots, base_dir=base_dir, fetch=args.fetch, max_depth=args.max_depth)
        except (OSError, ValueError) as exc:
            print(f"version-drift inbox: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            _print_inbox(result)
        return 0
    if args.command == "explain":
        try:
            if args.paths:
                projects = sorted(
                    {Path(path).expanduser().resolve() for path in args.paths},
                    key=str,
                )
            else:
                roots = resolve_roots([])
                projects = discover_projects(roots, max_depth=args.max_depth)
            reports = [
                inspect_project(str(path), fetch=False, optional_locks=False)
                for path in projects
            ]
            result = explain_reports(reports)
        except (OSError, ValueError) as exc:
            print(f"version-drift explain: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            _print_explain(result)
        return 0
    if args.command == "history":
        try:
            result = history(args.paths, base_dir=base_dir, limit=args.limit, events=args.event)
        except OSError as exc:
            print(f"version-drift history: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"version-drift history: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            _print_history(result)
        return 0
    if args.command == "inspect":
        report = inspect_project(args.path, fetch=args.fetch)
        try:
            record_event(report, base_dir=base_dir, event="inspect")
        except (OSError, UnicodeError, ValueError, TypeError):
            report["ok"] = False
            report.setdefault("reasons", []).append("event_write_failed")
            report["reason_codes"] = list(report["reasons"])
        print(json.dumps(report, ensure_ascii=False, sort_keys=True) if args.json else report)
        return 0 if report.get("ok") else 1

    paths = args.paths
    max_depth = 0 if len(paths) == 1 and (Path(paths[0]).expanduser() / ".git").exists() else args.max_depth
    if args.plan:
        result = plan_projects(paths, base_dir=base_dir, fetch=args.fetch, max_depth=max_depth)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            _print_plan(result)
        return 1 if result["outcome"] in {"partial", "failed"} else 0
    if len(paths) == 1 and (Path(paths[0]).expanduser() / ".git").exists():
        result = sync_projects(paths, base_dir=base_dir, apply=args.apply, fetch=args.fetch, max_depth=0)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            _print_sync(result, args.apply)
        if result["outcome"] in {"partial", "failed"}:
            return 3
        item = result["projects"][0]
        return 0 if item.get("applied") or (not args.apply and not item.get("blocked")) else 1
    result = sync_projects(paths, base_dir=base_dir, apply=args.apply, fetch=args.fetch, max_depth=args.max_depth)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        _print_sync(result, args.apply)
    return 3 if result["outcome"] in {"partial", "failed"} else 0


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
