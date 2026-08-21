import json
import subprocess
import sys
from importlib import metadata

import pytest
from pathlib import Path

import version_drift.core as core
from version_drift import inspect_project, record_event, scan_projects, sync_projects
from version_drift.cli import main


def _git(path: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return proc.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "init")


def _remote_clone(tmp_path: Path) -> tuple[Path, Path, Path]:
    seed = tmp_path / "seed"
    remote = tmp_path / "remote.git"
    clone = tmp_path / "clone"
    _init_repo(seed)
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "HEAD")
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test User")
    return seed, remote, clone


def _commit(path: Path, filename: str, text: str, message: str) -> None:
    (path / filename).write_text(text, encoding="utf-8")
    _git(path, "add", filename)
    _git(path, "commit", "-m", message)


def test_default_roots_uses_version_drift_env(monkeypatch, tmp_path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    monkeypatch.setenv("VERSION_DRIFT_ROOTS", f"{one}:{two}")
    monkeypatch.setenv("MEMKRAFT_PROJECT_SYNC_ROOTS", "/must/not/be/used")

    assert core.default_roots() == [str(one), str(two)]


def test_history_reads_events_newest_first_and_filters_components(tmp_path):
    state = tmp_path / "state"
    event_file = state / ".version-drift" / "events.jsonl"
    event_file.parent.mkdir(parents=True)
    event_file.write_text(
        json.dumps({"event": "scan", "path": str(tmp_path / "a"), "state": "synced"}) + "\n"
        + "blank\n\n"
        + json.dumps({"event": "sync_applied", "path": str(tmp_path / "ab"), "state": "behind_clean"}) + "\n",
        encoding="utf-8",
    )
    result = core.history(base_dir=str(state), paths=[str(tmp_path / "a")], limit=0)
    assert result["schema"] == "version-drift/history/1"
    assert result["source_exists"] is True
    assert result["malformed_lines"] == 2
    assert [item["event"] for item in result["events"]] == ["scan"]
    assert result["events"][0]["sequence"] == 1


def test_history_missing_file_and_negative_limit(tmp_path):
    result = core.history(base_dir=str(tmp_path / "missing"))
    assert result["source_exists"] is False and result["events"] == []
    with pytest.raises(ValueError):
        core.history(base_dir=str(tmp_path), limit=-1)


def test_discovery_does_not_follow_directory_symlinks(tmp_path):
    inside = tmp_path / "inside"
    outside = tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    _init_repo(outside / "repo")
    (inside / "escape").symlink_to(outside, target_is_directory=True)
    assert core.discover_projects([str(inside)]) == []


def test_negative_depth_rejected_by_public_apis(tmp_path):
    for call in (
        lambda: core.discover_projects([str(tmp_path)], -1),
        lambda: core.scan_projects([str(tmp_path)], max_depth=-1),
        lambda: core.sync_projects([str(tmp_path)], max_depth=-1),
    ):
        with pytest.raises(ValueError):
            call()


def test_inspect_and_event_store(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    report = inspect_project(str(repo))
    assert report["schema"] == "version-drift/1"
    assert "missing_upstream" in report["reasons"]
    event = record_event(report, base_dir=str(tmp_path / "state"), event="inspect")
    assert event["event"] == "inspect"
    assert (tmp_path / "state" / ".version-drift" / "events.jsonl").exists()


def test_default_event_store_is_outside_the_current_git_repository(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("VERSION_DRIFT_DIR", raising=False)

    report = inspect_project(str(repo))
    record_event(report, event="inspect")

    assert not (repo / ".version-drift").exists()
    if sys.platform == "darwin":
        expected = home / "Library" / "Application Support" / "VersionDrift" / "events.jsonl"
    else:
        expected = home / ".local" / "state" / "version-drift" / "events.jsonl"
    assert expected.exists()


def test_scan_measures_working_file_changes_instead_of_hardcoding_zero(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    real_record_event = core.record_event

    def mutating_record_event(report, base_dir=None, event="scan"):
        (repo / "unexpected.txt").write_text("changed\n", encoding="utf-8")
        return real_record_event(report, base_dir=base_dir, event=event)

    monkeypatch.setattr(core, "record_event", mutating_record_event)

    result = scan_projects([str(repo)], base_dir=str(tmp_path / "state"))

    assert result["summary"]["working_files_changed"] == 1
    assert result["summary"]["working_files_changed_paths"] == [str(repo / "unexpected.txt")]


def test_scan_detects_content_change_when_porcelain_status_is_unchanged(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    tracked = repo / "README.md"
    tracked.write_text("dirty before\n", encoding="utf-8")
    real_record_event = core.record_event

    def mutating_record_event(report, base_dir=None, event="scan"):
        tracked.write_text("dirty after\n", encoding="utf-8")
        return real_record_event(report, base_dir=base_dir, event=event)

    monkeypatch.setattr(core, "record_event", mutating_record_event)

    result = scan_projects([str(repo)], base_dir=str(tmp_path / "state"))

    assert result["summary"]["working_files_changed"] == 1
    assert result["summary"]["working_files_changed_paths"] == [str(tracked)]


def test_relative_xdg_state_home_is_ignored(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(core.sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", ".state")

    assert core._event_path(None) == home / ".local" / "state" / "version-drift" / "events.jsonl"


def test_inspect_classifies_synced_repository(tmp_path):
    _, _, clone = _remote_clone(tmp_path)

    report = inspect_project(str(clone))

    assert report["state"] == "synced"
    assert report["ok"] is True
    assert report["ahead"] == 0
    assert report["behind"] == 0


def test_inspect_classifies_clean_behind_repository(tmp_path):
    seed, _, clone = _remote_clone(tmp_path)
    _commit(seed, "remote.txt", "remote\n", "remote")
    _git(seed, "push")

    report = inspect_project(str(clone), fetch=True)

    assert report["state"] == "behind_clean"
    assert report["behind"] == 1
    assert report["safe_to_update"] is True


def test_fetch_failure_is_fail_closed_even_when_tracking_ref_is_behind(tmp_path):
    seed, _, clone = _remote_clone(tmp_path)
    _commit(seed, "remote.txt", "remote\n", "remote")
    _git(seed, "push")
    _git(clone, "fetch")
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "missing-remote.git"))

    report = inspect_project(str(clone), fetch=True)

    assert "fetch_failed" in report["reasons"]
    assert report["state"] == "protected"
    assert report["safe_to_update"] is False


def test_inspect_protects_untracked_work(tmp_path):
    _, _, clone = _remote_clone(tmp_path)
    untracked = clone / "notes.txt"
    untracked.write_text("keep me\n", encoding="utf-8")

    report = inspect_project(str(clone))

    assert report["state"] == "protected"
    assert "dirty_worktree" in report["reasons"]
    assert report["ok"] is False
    assert report["safe_to_update"] is False
    assert untracked.read_text(encoding="utf-8") == "keep me\n"


def test_inspect_protects_dirty_repository_that_is_behind(tmp_path):
    seed, _, clone = _remote_clone(tmp_path)
    (clone / "notes.txt").write_text("keep me\n", encoding="utf-8")
    _commit(seed, "remote.txt", "remote\n", "remote")
    _git(seed, "push")

    report = inspect_project(str(clone), fetch=True)

    assert report["state"] == "protected"
    assert "dirty_worktree" in report["reasons"]
    assert report["behind"] == 1
    assert report["ok"] is False
    assert report["safe_to_update"] is False


def test_inspect_fails_closed_when_status_is_unavailable(monkeypatch, tmp_path):
    _, _, clone = _remote_clone(tmp_path)
    real = core._run_git

    def fail_status(repo, args, timeout_s=20.0):
        if list(args) == ["status", "--porcelain=v1", "-uall"]:
            return core.GitResult(False, stderr="status unavailable", returncode=128)
        return real(repo, args, timeout_s)

    monkeypatch.setattr(core, "_run_git", fail_status)
    report = inspect_project(str(clone))

    assert report["state"] == "protected"
    assert "worktree_status_unavailable" in report["reasons"]
    assert report["ok"] is False
    assert report["safe_to_update"] is False


def test_inspect_classifies_local_ahead(tmp_path):
    _, _, clone = _remote_clone(tmp_path)
    _commit(clone, "local.txt", "local\n", "local")

    report = inspect_project(str(clone))

    assert report["state"] == "ahead"
    assert report["ahead"] == 1
    assert report["safe_to_update"] is False


def test_inspect_classifies_diverged(tmp_path):
    seed, _, clone = _remote_clone(tmp_path)
    _commit(seed, "remote.txt", "remote\n", "remote")
    _git(seed, "push")
    _commit(clone, "local.txt", "local\n", "local")

    report = inspect_project(str(clone), fetch=True)

    assert report["state"] == "diverged"
    assert report["ahead"] == 1
    assert report["behind"] == 1
    assert report["safe_to_update"] is False


def test_scan_does_not_change_working_files(tmp_path):
    _, _, clone = _remote_clone(tmp_path)
    local = clone / "local.txt"
    local.write_text("precious\n", encoding="utf-8")
    before_head = _git(clone, "rev-parse", "HEAD")
    before_status = _git(clone, "status", "--porcelain=v1", "-uall")

    scan_projects([str(tmp_path)], base_dir=str(tmp_path / "state"))

    assert local.read_text(encoding="utf-8") == "precious\n"
    assert _git(clone, "rev-parse", "HEAD") == before_head
    assert _git(clone, "status", "--porcelain=v1", "-uall") == before_status


def test_cli_accepts_positional_roots_and_scan_drift_is_success(tmp_path, capsys):
    repo = tmp_path / "repo"
    _init_repo(repo)

    rc = main(["--base-dir", str(tmp_path / "state"), "scan", str(tmp_path)])

    out = capsys.readouterr().out
    assert rc == 0
    assert "VersionDrift scanned 1 repository" in out
    assert "local work protected" in out or "no upstream" in out
    assert "Working files changed: 0" in out


def test_cli_check_returns_one_when_drift_exists(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)

    rc = main(["--base-dir", str(tmp_path / "state"), "scan", str(tmp_path), "--check"])

    assert rc == 1


def test_cli_json_scan_remains_machine_readable(tmp_path, capsys):
    repo = tmp_path / "repo"
    _init_repo(repo)

    rc = main(["--base-dir", str(tmp_path / "state"), "scan", str(tmp_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["summary"]["total"] == 1
    assert payload["summary"]["working_files_changed"] == 0


def test_sync_projects_dry_run_lists_safe_and_protected(tmp_path):
    seed, _, safe = _remote_clone(tmp_path)
    _commit(seed, "remote.txt", "remote\n", "remote")
    _git(seed, "push")
    protected = tmp_path / "protected"
    subprocess.run(["git", "clone", str(tmp_path / "remote.git"), str(protected)], check=True, capture_output=True)
    (protected / "notes.txt").write_text("keep\n", encoding="utf-8")

    result = sync_projects([str(tmp_path)], base_dir=str(tmp_path / "state"), apply=False, fetch=True)

    assert result["summary"]["safe"] == 1
    assert result["summary"]["protected"] >= 1
    assert result["summary"]["applied"] == 0
    assert not (safe / "remote.txt").exists()
    assert (protected / "notes.txt").read_text(encoding="utf-8") == "keep\n"


def test_sync_projects_applies_only_clean_fast_forwards(tmp_path):
    seed, _, safe = _remote_clone(tmp_path)
    _commit(seed, "remote.txt", "remote\n", "remote")
    _git(seed, "push")
    dirty = tmp_path / "dirty"
    subprocess.run(["git", "clone", str(tmp_path / "remote.git"), str(dirty)], check=True, capture_output=True)
    (dirty / "notes.txt").write_text("keep\n", encoding="utf-8")
    _commit(seed, "second.txt", "second\n", "second")
    _git(seed, "push")

    result = sync_projects([str(tmp_path)], base_dir=str(tmp_path / "state"), apply=True, fetch=True)

    assert result["summary"]["applied"] == 1
    assert result["summary"]["working_files_changed"] == 2
    assert (safe / "remote.txt").exists()
    assert (safe / "second.txt").exists()
    assert (dirty / "notes.txt").read_text(encoding="utf-8") == "keep\n"


def test_cli_single_repo_sync_apply_reports_changed_files(tmp_path, capsys):
    seed, _, clone = _remote_clone(tmp_path)
    _commit(seed, "remote.txt", "remote\n", "remote")
    _git(seed, "push")

    rc = main(["--base-dir", str(tmp_path / "state"), "sync", str(clone), "--apply", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["summary"]["applied"] == 1
    assert payload["summary"]["working_files_changed"] == 1
    assert payload["summary"]["working_files_changed_paths"] == [str(clone / "remote.txt")]


def test_sync_project_aborts_if_state_changes_before_apply(monkeypatch, tmp_path):
    before = {
        "schema": "version-drift/1",
        "path": str(tmp_path / "repo"),
        "state": "behind_clean",
        "safe_to_update": True,
        "head": "abc",
        "status_fingerprint": "clean",
        "ok": False,
        "reasons": ["local_behind_upstream"],
    }
    changed = dict(before, state="protected", safe_to_update=False, status_fingerprint="dirty")
    reports = iter([before, changed])
    monkeypatch.setattr(core, "inspect_project", lambda *args, **kwargs: next(reports))
    calls = []
    monkeypatch.setattr(core, "_run_git", lambda repo, args, timeout_s=20.0: calls.append(args))

    result = core.sync_project(str(tmp_path / "repo"), apply=True, fetch=False)

    assert result["applied"] is False
    assert result["reason"] == "state_changed_before_apply"
    assert ["pull", "--ff-only"] not in calls


def test_sync_uses_only_documented_non_destructive_git_commands(monkeypatch, tmp_path):
    _, _, clone = _remote_clone(tmp_path)
    seen = []
    real = core._run_git

    def recording(repo, args, timeout_s=20.0):
        seen.append(tuple(args))
        return real(repo, args, timeout_s)

    monkeypatch.setattr(core, "_run_git", recording)
    core.sync_project(str(clone), apply=True, fetch=False)

    forbidden = {"reset", "stash", "clean", "merge", "rebase", "checkout", "switch", "push"}
    assert not any(command and command[0] in forbidden for command in seen)


def test_event_log_contains_sync_decisions(tmp_path):
    _, _, clone = _remote_clone(tmp_path)

    core.sync_project(str(clone), base_dir=str(tmp_path / "state"), apply=True, fetch=False)

    path = tmp_path / "state" / ".version-drift" / "events.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event"] == "sync_blocked"
    assert events[-1]["schema"] == "version-drift/1"


def test_cli_inspect_json_preserves_blocked_exit_code(tmp_path, capsys):
    repo = tmp_path / "repo"
    _init_repo(repo)
    rc = main(["--base-dir", str(tmp_path / "state"), "inspect", str(repo), "--json"])
    assert rc == 1
    out = capsys.readouterr().out
    assert '"schema": "version-drift/1"' in out
    assert '"state": "protected"' in out


def test_cli_version_comes_from_installed_package_metadata(monkeypatch, capsys):
    monkeypatch.setattr("version_drift.cli.metadata.version", lambda name: "9.8.7")

    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0

    assert capsys.readouterr().out.strip() == "version-drift 9.8.7"


def test_cli_version_falls_back_to_source_version_without_distribution_metadata(monkeypatch, capsys):
    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr("version_drift.cli.metadata.version", missing)

    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0

    assert capsys.readouterr().out.strip() == "version-drift 0.5.0"
