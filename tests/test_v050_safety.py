import json
import os
import subprocess
from pathlib import Path

import pytest

import version_drift.core as core
from version_drift.cli import main
from version_drift.inbox import build_inbox


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-m", "initial")


def test_history_projects_newest_first_with_filters_and_counts(tmp_path):
    state = tmp_path / "state"
    source = state / ".version-drift" / "events.jsonl"
    source.parent.mkdir(parents=True)
    repo = tmp_path / "repo"
    sibling = tmp_path / "repo-other"
    rows = [
        {"event": "scan", "checked_at": "2026-01-01T00:00:00+00:00", "path": str(repo), "state": "synced"},
        "not-json",
        {"event": "future_event", "checked_at": "2026-01-02T00:00:00+00:00", "path": str(repo / "child"), "state": "protected"},
        {"event": "future_event", "checked_at": "2026-01-03T00:00:00+00:00", "path": str(sibling), "state": "ahead"},
    ]
    source.write_text("\n".join(json.dumps(row) if isinstance(row, dict) else row for row in rows) + "\n", encoding="utf-8")

    result = core.history([str(repo)], base_dir=str(state), events=["future_event"], limit=20)

    assert result["schema"] == "version-drift/history/1"
    assert result["filters"] == {"paths": [str(repo.resolve())], "events": ["future_event"], "limit": 20}
    assert result["counts"] == {"source_lines": 4, "malformed_lines": 1, "matched": 1, "returned": 1}
    assert result["events"][0]["sequence"] == 3
    assert result["events"][0]["event"] == "future_event"
    assert result["events"][0]["ahead"] is None


def test_history_missing_source_does_not_create_state(tmp_path):
    state = tmp_path / "missing"

    result = core.history(base_dir=str(state))

    assert result["source_exists"] is False
    assert result["counts"] == {"source_lines": 0, "malformed_lines": 0, "matched": 0, "returned": 0}
    assert not state.exists()


def test_history_cli_is_byte_read_only_and_never_calls_git_or_records(monkeypatch, tmp_path, capsys):
    state = tmp_path / "state"
    source = state / ".version-drift" / "events.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"event": "scan", "path": str(tmp_path / "repo")}) + "\n", encoding="utf-8")
    before = source.read_bytes()
    monkeypatch.setattr(core, "record_event", lambda *a, **k: pytest.fail("history recorded an event"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("history invoked a subprocess"))

    rc = main(["--base-dir", str(state), "history", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["counts"]["returned"] == 1
    assert source.read_bytes() == before
    assert not (source.parent / "inbox_snapshot.json").exists()


def test_history_cli_reports_malformed_lines_and_footer(tmp_path, capsys):
    state = tmp_path / "state"
    source = state / ".version-drift" / "events.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(b'{"event":"scan","path":"/tmp/repo"}\n{"torn":')

    rc = main(["--base-dir", str(state), "history"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Skipped 1 malformed event line." in out
    assert out.rstrip().endswith("Nothing was changed.")


def test_history_skips_invalid_utf8_line(tmp_path):
    state = tmp_path / "state"
    source = state / ".version-drift" / "events.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(
        b'{"event":"scan","path":"/tmp/repo"}\n'
        b'{"event":"scan","path":"\xff"}\n'
    )

    result = core.history(base_dir=str(state))

    assert result["counts"] == {
        "source_lines": 2,
        "malformed_lines": 1,
        "matched": 1,
        "returned": 1,
    }


def test_history_unreadable_source_is_clean_exit_one(monkeypatch, tmp_path, capsys):
    state = tmp_path / "state"
    source = state / ".version-drift" / "events.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text("{}\n", encoding="utf-8")
    real_open = Path.open

    def denied(self, *args, **kwargs):
        if self == source:
            raise OSError("permission denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    rc = main(["--base-dir", str(state), "history", "--json"])

    assert rc == 1
    assert "permission denied" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["scan", "inbox", "explain"])
def test_cli_rejects_negative_max_depth_with_usage_exit(command):
    assert main([command, "--max-depth", "-1"]) == 2


def test_sync_cli_rejects_negative_max_depth_with_usage_exit(tmp_path):
    assert main(["sync", str(tmp_path), "--max-depth", "-1"]) == 2


@pytest.mark.parametrize(
    "call",
    [
        lambda root: core.discover_projects([str(root)], max_depth=-1),
        lambda root: core.scan_projects([str(root)], max_depth=-1),
        lambda root: core.sync_projects([str(root)], max_depth=-1),
        lambda root: build_inbox([str(root)], max_depth=-1),
    ],
)
def test_public_apis_reject_negative_max_depth(call, tmp_path):
    with pytest.raises(ValueError, match="max depth must be non-negative"):
        call(tmp_path)


def test_discovery_rejects_symlink_as_explicit_root(tmp_path):
    outside = tmp_path / "outside"
    _init_repo(outside)
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(outside, target_is_directory=True)

    assert core.discover_projects([str(linked_root)]) == []


def test_discovery_rejects_symlink_in_explicit_root_components(tmp_path):
    outside = tmp_path / "outside"
    repo = outside / "nested" / "repo"
    _init_repo(repo)
    inside = tmp_path / "inside"
    inside.mkdir()
    (inside / "escape").symlink_to(outside, target_is_directory=True)

    assert core.discover_projects([str(inside / "escape" / "nested")]) == []


def test_inspect_fails_closed_on_malformed_ahead_behind(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    real = core._run_git

    def malformed(path, args, timeout_s=20.0, **kwargs):
        command = list(args)
        if command == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]:
            return core.GitResult(True, stdout="origin/main")
        if command == ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"]:
            return core.GitResult(True, stdout="-1 2")
        return real(path, args, timeout_s=timeout_s, **kwargs)

    monkeypatch.setattr(core, "_run_git", malformed)
    report = core.inspect_project(str(repo))

    assert report["state"] == "protected"
    assert "ahead_behind_unavailable" in report["reasons"]


def test_scan_does_not_refresh_git_index(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    index = Path(_git(repo, "rev-parse", "--git-path", "index"))
    if not index.is_absolute():
        index = repo / index
    tracked = repo / "tracked.txt"
    original = tracked.stat()
    os.utime(tracked, ns=(original.st_atime_ns, original.st_mtime_ns + 2_000_000_000))
    before = (index.read_bytes(), index.stat().st_mtime_ns)

    core.scan_projects([str(repo)], base_dir=str(tmp_path / "state"), max_depth=0)

    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


def test_untracked_symlink_is_not_dereferenced_and_fifo_is_not_opened(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    outside = tmp_path / "outside-secret"
    outside.write_text("first", encoding="utf-8")
    (repo / "link").symlink_to(outside)
    os.mkfifo(repo / "pipe")

    first = core._working_content_snapshot(repo)
    outside.write_text("second", encoding="utf-8")
    second = core._working_content_snapshot(repo)

    assert first[2] is True
    assert first == second


@pytest.mark.parametrize("replacement", ["fifo", "symlink"])
def test_regular_file_replaced_before_open_is_not_read(monkeypatch, tmp_path, replacement):
    repo = tmp_path / "repo"
    _init_repo(repo)
    target = repo / "untracked"
    target.write_text("ordinary", encoding="utf-8")
    outside = tmp_path / "outside-secret"
    outside.write_text("secret", encoding="utf-8")
    real_open = os.open
    replaced = False

    def replace_then_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if Path(path) == target and not replaced:
            replaced = True
            target.unlink()
            if replacement == "fifo":
                os.mkfifo(target)
            else:
                target.symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_then_open)
    snapshot = core._working_content_snapshot(repo)

    assert replaced is True
    assert snapshot[2] is False


def test_regular_untracked_file_fails_closed_without_nofollow(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "untracked").write_text("ordinary", encoding="utf-8")
    monkeypatch.delattr(os, "O_NOFOLLOW")
    real_open = os.open

    def reject_target_open(path, flags, *args, **kwargs):
        if Path(path) == repo / "untracked":
            pytest.fail("untracked file was opened without O_NOFOLLOW")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", reject_target_open)

    assert core._working_content_snapshot(repo)[2] is False
