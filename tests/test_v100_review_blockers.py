import json
import os
import subprocess
from pathlib import Path

import pytest

import version_drift.cli as cli
import version_drift.core as core


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def _eligible(path: Path, *, state="behind_clean", ok=False):
    eligible = state == "behind_clean"
    return {
        "path": str(path.resolve()), "state": state, "ok": ok,
        "safe_to_update": eligible, "eligibility": "eligible" if eligible else "blocked",
        "relation": "behind" if eligible else "unknown", "head": "old",
        "upstream": "origin/main", "status_fingerprint": "clean",
        "reasons": ["local_behind_upstream"] if eligible else [],
        "reason_codes": ["local_behind_upstream"] if eligible else [], "actions": [],
    }


@pytest.mark.parametrize("kind", ["missing", "non_git", "bare"])
def test_explicit_uninspectable_sync_target_is_failed_and_nonzero(tmp_path, kind, capsys):
    target = tmp_path / kind
    if kind == "non_git":
        target.mkdir()
        (target / "file").write_text("not a repository\n", encoding="utf-8")
    elif kind == "bare":
        subprocess.run(["git", "init", "--bare", str(target)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    code = cli.main(["--base-dir", str(tmp_path / "state"), "sync", str(target),
                     "--no-fetch", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 3
    assert payload["outcome"] == "failed"
    assert payload["summary"]["failed"] == 1
    assert payload["projects"][0]["blocked"] is True


def test_empty_ordinary_root_is_a_complete_no_project_result(tmp_path):
    result = core.scan_projects([str(tmp_path)], base_dir=str(tmp_path / "state"))
    assert result["outcome"] == "complete"
    assert result["summary"]["total"] == 0


def test_scan_failed_is_nonzero_without_check(monkeypatch, capsys):
    payload = {"schema": core.SCAN_SCHEMA, "outcome": "failed",
               "summary": {"total": 1, "ok": False}, "projects": []}
    monkeypatch.setattr(cli, "scan_projects", lambda *args, **kwargs: payload)
    assert cli.main(["scan", ".", "--json"]) == 3
    capsys.readouterr()


def test_scan_partial_is_nonzero_without_check(monkeypatch, capsys):
    payload = {"schema": core.SCAN_SCHEMA, "outcome": "partial",
               "summary": {"total": 2, "ok": False}, "projects": []}
    monkeypatch.setattr(cli, "scan_projects", lambda *args, **kwargs: payload)
    assert cli.main(["scan", ".", "--json"]) == 3
    capsys.readouterr()


def test_lock_path_io_failure_is_structured_and_does_not_pull(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    monkeypatch.setattr(core, "inspect_project", lambda *args, **kwargs: _eligible(repo))
    monkeypatch.setattr(core, "_acquire_apply_lock", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("denied")))
    pulls = []
    monkeypatch.setattr(core, "_run_git", lambda *args, **kwargs: pulls.append(args))

    result = core.sync_project(str(repo), base_dir=str(tmp_path / "state"), apply=True, fetch=False)

    assert result["blocked"] is True and result["applied"] is False
    assert result["reason"] == "apply_lock_io_failed"
    assert pulls == []


def test_event_write_failure_before_pull_is_structured_and_does_not_pull(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    reports = iter([_eligible(repo), _eligible(repo)])
    monkeypatch.setattr(core, "inspect_project", lambda *args, **kwargs: next(reports))
    monkeypatch.setattr(core, "record_event", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("full")))
    pulls = []
    monkeypatch.setattr(core, "_run_git", lambda *args, **kwargs: pulls.append(args))

    result = core.sync_project(str(repo), base_dir=str(tmp_path / "state"), apply=True, fetch=False)

    assert result["blocked"] is True and result["applied"] is False
    assert result["reason"] == "event_write_failed"
    assert pulls == []


def test_event_write_failure_after_successful_pull_is_unknown(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    reports = iter([_eligible(repo), _eligible(repo), _eligible(repo, state="synced", ok=True)])
    monkeypatch.setattr(core, "inspect_project", lambda *args, **kwargs: next(reports))
    calls = 0

    def events(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("full")

    monkeypatch.setattr(core, "record_event", events)
    monkeypatch.setattr(core, "_run_git", lambda *args, **kwargs: core.GitResult(True, "updated"))

    result = core.sync_project(str(repo), base_dir=str(tmp_path / "state"), apply=True, fetch=False)

    assert result["blocked"] is True and result["applied"] is False
    assert result["reason"] == "fast_forward_outcome_unknown"


def test_pull_success_with_unverifiable_post_state_is_legacy_blocked(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    reports = iter([_eligible(repo), _eligible(repo), _eligible(repo, state="protected")])
    monkeypatch.setattr(core, "inspect_project", lambda *args, **kwargs: next(reports))
    monkeypatch.setattr(core, "record_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(core, "_run_git", lambda *args, **kwargs: core.GitResult(True, "updated"))

    result = core.sync_project(str(repo), base_dir=str(tmp_path / "state"), apply=True, fetch=False)
    assert result["reason"] == "fast_forward_outcome_unknown"
    assert result["blocked"] is True and result["applied"] is False


def test_gitlink_without_gitmodules_blocks_and_never_pulls(monkeypatch, tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    _git(child, "init")
    _git(child, "config", "user.email", "test@example.com")
    _git(child, "config", "user.name", "Test")
    (child / "a").write_text("a\n", encoding="utf-8")
    _git(child, "add", "a")
    _git(child, "commit", "-m", "initial")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "update-index", "--add", "--cacheinfo", "160000," + _git(child, "rev-parse", "HEAD") + ",vendor/child")
    _git(repo, "commit", "-m", "gitlink only")
    pulls = []
    real = core._run_git

    def spy(path, args, **kwargs):
        if list(args)[:1] == ["pull"]:
            pulls.append(args)
        return real(path, args, **kwargs)

    monkeypatch.setattr(core, "_run_git", spy)
    report = core.inspect_project(str(repo))
    result = core.sync_project(str(repo), base_dir=str(tmp_path / "state"), apply=True, fetch=False)

    assert "contains_submodules" in report["reason_codes"]
    assert result["blocked"] is True
    assert pulls == []


def test_post_inspection_io_failure_after_pull_is_unknown(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    calls = 0

    def inspect(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("cannot inspect")
        return _eligible(repo)

    monkeypatch.setattr(core, "inspect_project", inspect)
    monkeypatch.setattr(core, "record_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(core, "_run_git", lambda *args, **kwargs: core.GitResult(True, "updated"))

    result = core.sync_project(str(repo), base_dir=str(tmp_path / "state"), apply=True, fetch=False)
    assert result["reason"] == "fast_forward_outcome_unknown"
    assert result["blocked"] is True and result["applied"] is False


def test_keyboard_interrupt_from_event_writer_is_not_swallowed(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    monkeypatch.setattr(core, "inspect_project", lambda *args, **kwargs: _eligible(repo))
    monkeypatch.setattr(core, "record_event", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        core.sync_project(str(repo), base_dir=str(tmp_path / "state"), apply=False, fetch=False)


def test_inspect_event_write_failure_is_structured(monkeypatch, tmp_path, capsys):
    repo = tmp_path / "repo"
    monkeypatch.setattr(cli, "inspect_project", lambda *args, **kwargs: _eligible(repo))
    monkeypatch.setattr(cli, "record_event", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("full")))

    assert cli.main(["inspect", str(repo), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "event_write_failed" in payload["reason_codes"]


def test_nested_discovery_failure_is_observed_alongside_visible_repo(monkeypatch, tmp_path):
    repo = tmp_path / "a-repo"
    (repo / ".git").mkdir(parents=True)
    denied = tmp_path / "z-denied"
    denied.mkdir()
    real_iterdir = Path.iterdir

    def iterdir(path):
        if path == denied:
            raise OSError("denied")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(core, "inspect_project", lambda *args, **kwargs: _eligible(repo))
    monkeypatch.setattr(core, "record_event", lambda *args, **kwargs: None)

    scan = core.scan_projects([str(tmp_path)], base_dir=str(tmp_path / "scan-state"), fetch=False)
    plan = core.plan_projects([str(tmp_path)], base_dir=str(tmp_path / "plan-state"), fetch=False)
    sync = core.sync_projects([str(tmp_path)], base_dir=str(tmp_path / "sync-state"), fetch=False)

    assert core.discover_projects([str(tmp_path)]) == [repo]
    assert scan["outcome"] == "partial"
    assert plan["outcome"] == "partial"
    assert sync["outcome"] in {"partial", "failed"}
    assert any(item["path"] == str(denied) and "scope_inspection_failed" in item["reasons"]
               for item in scan["projects"])
    assert any(item["path"] == str(denied) and "scope_inspection_failed" in item["reason_codes"]
               for item in plan["repositories"])
    assert any(item["before"]["path"] == str(denied) for item in sync["projects"])


def test_all_nested_discovery_scopes_failing_is_failed(monkeypatch, tmp_path):
    denied = tmp_path / "denied"
    denied.mkdir()
    real_iterdir = Path.iterdir

    def iterdir(path):
        if path == denied:
            raise OSError("denied")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(core, "record_event", lambda *args, **kwargs: None)

    assert core.scan_projects([str(denied)], base_dir=str(tmp_path / "state"), fetch=False)["outcome"] == "failed"
    assert core.plan_projects([str(denied)], base_dir=str(tmp_path / "state"), fetch=False)["outcome"] == "failed"


def test_nested_classification_failure_is_observed(monkeypatch, tmp_path):
    denied = tmp_path / "denied"
    denied.mkdir()
    real_is_dir = Path.is_dir

    def is_dir(path):
        if path == denied:
            raise OSError("cannot classify")
        return real_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    monkeypatch.setattr(core, "record_event", lambda *args, **kwargs: None)
    result = core.scan_projects([str(tmp_path)], base_dir=str(tmp_path / "state"), fetch=False)

    assert result["outcome"] == "failed"
    assert result["projects"][0]["path"] == str(denied)


def test_nested_resolve_failure_is_observed(monkeypatch, tmp_path):
    denied = tmp_path / "denied"
    denied.mkdir()
    real_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == denied:
            raise OSError("cannot resolve")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(core, "record_event", lambda *args, **kwargs: None)
    result = core.scan_projects([str(tmp_path)], base_dir=str(tmp_path / "state"), fetch=False)

    assert result["outcome"] == "failed"
    assert result["projects"][0]["path"] == str(denied)


def test_record_event_sanitizes_surrogateescaped_local_strings(tmp_path):
    report = {
        "path": str(tmp_path / "repo") + "\udcff",
        "remote_url": "ssh://example/\udcfe",
        "diagnostic": "ordinary 한글 \udcfd",
    }

    core.record_event(report, base_dir=str(tmp_path / "state"), event="inspect")

    raw = (tmp_path / "state" / ".version-drift" / "events.jsonl").read_bytes()
    decoded = raw.decode("utf-8")
    payload = json.loads(decoded)
    assert payload["diagnostic"] == "ordinary 한글 �"
    assert "\udcff" not in decoded and "\udcfe" not in decoded and "\udcfd" not in decoded


@pytest.mark.parametrize("error", [
    UnicodeEncodeError("utf-8", "\udcff", 0, 1, "surrogate"),
    ValueError("bad event"),
    TypeError("bad event"),
])
def test_event_serialization_failure_before_pull_is_structured(monkeypatch, tmp_path, error):
    repo = tmp_path / "repo"
    monkeypatch.setattr(core, "inspect_project", lambda *args, **kwargs: _eligible(repo))
    monkeypatch.setattr(core, "record_event", lambda *args, **kwargs: (_ for _ in ()).throw(error))
    pulls = []
    monkeypatch.setattr(core, "_run_git", lambda *args, **kwargs: pulls.append(args))

    result = core.sync_project(str(repo), base_dir=str(tmp_path / "state"), apply=True, fetch=False)

    assert result["reason"] == "event_write_failed"
    assert result["blocked"] is True
    assert pulls == []


def test_unicode_event_failure_after_pull_is_unknown(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    reports = iter([_eligible(repo), _eligible(repo), _eligible(repo, state="synced", ok=True)])
    monkeypatch.setattr(core, "inspect_project", lambda *args, **kwargs: next(reports))
    calls = 0

    def events(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise UnicodeEncodeError("utf-8", "\udcff", 0, 1, "surrogate")

    monkeypatch.setattr(core, "record_event", events)
    monkeypatch.setattr(core, "_run_git", lambda *args, **kwargs: core.GitResult(True, "updated"))

    result = core.sync_project(str(repo), base_dir=str(tmp_path / "state"), apply=True, fetch=False)

    assert result["reason"] == "fast_forward_outcome_unknown"
    assert result["blocked"] is True and result["applied"] is False


@pytest.mark.parametrize("operation", ["scan", "plan", "inspect"])
def test_event_serialization_errors_are_structured_across_read_paths(monkeypatch, tmp_path, capsys, operation):
    repo = tmp_path / "repo"
    monkeypatch.setattr(core, "discover_projects", lambda *args, **kwargs: [repo])
    monkeypatch.setattr(core, "_repository_snapshots", lambda paths: {})
    monkeypatch.setattr(core, "inspect_project", lambda *args, **kwargs: _eligible(repo))
    monkeypatch.setattr(core, "record_event", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad event")))

    if operation == "scan":
        assert core.scan_projects([str(tmp_path)])["outcome"] == "failed"
    elif operation == "plan":
        assert core.plan_projects([str(tmp_path)], fetch=False)["outcome"] == "failed"
    else:
        monkeypatch.setattr(cli, "inspect_project", lambda *args, **kwargs: _eligible(repo))
        monkeypatch.setattr(cli, "record_event", core.record_event)
        assert cli.main(["inspect", str(repo), "--json"]) == 1
        assert "event_write_failed" in json.loads(capsys.readouterr().out)["reason_codes"]
