import json
import subprocess
from pathlib import Path

import pytest

import version_drift.core as core
from version_drift.cli import main


AUTHORIZATION = "This plan grants no authorization; apply independently reinspects every repository."


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
    (path / "README.md").write_text("initial\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def _remote_clone(tmp_path: Path) -> tuple[Path, Path]:
    seed = tmp_path / "seed"
    remote = tmp_path / "remote.git"
    clone = tmp_path / "clone"
    _init_repo(seed)
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "HEAD")
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    return seed, clone


def _push_remote_change(seed: Path) -> None:
    (seed / "remote.txt").write_text("remote\n", encoding="utf-8")
    _git(seed, "add", "remote.txt")
    _git(seed, "commit", "-m", "remote")
    _git(seed, "push")


def test_plan_projects_has_exact_envelope_shape_and_records_one_decision_per_repo(monkeypatch, tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    reports = {
        str(first): {
            "path": str(first), "relation": "behind", "eligibility": "eligible",
            "reason_codes": ["local_behind_upstream"], "head": "aaa", "upstream": "origin/main",
            "ahead": 0, "behind": 2, "status_fingerprint": "clean-a",
        },
        str(second): {
            "path": str(second), "relation": "unknown", "eligibility": "unknown",
            "reason_codes": ["git_metadata_unreadable"],
        },
    }
    events = []
    monkeypatch.setattr(core, "discover_projects", lambda roots, max_depth=5: [first, second])
    monkeypatch.setattr(core, "inspect_project", lambda path, fetch=False: reports[path])
    monkeypatch.setattr(core, "record_event", lambda report, base_dir=None, event="scan": events.append((report, event)))

    result = core.plan_projects([str(tmp_path)], base_dir=str(tmp_path / "state"), fetch=False)

    assert set(result) == {
        "schema", "generated_at", "fetch_performed", "outcome", "summary",
        "repositories", "authorization",
    }
    assert result["schema"] == "version-drift/plan/1"
    assert result["generated_at"]
    assert result["fetch_performed"] is False
    assert result["outcome"] == "partial"
    assert result["summary"] == {"total": 2, "planned": 1, "blocked": 0, "unknown": 1}
    assert result["authorization"] == AUTHORIZATION
    assert [item["path"] for item in result["repositories"]] == [str(first), str(second)]
    assert set(result["repositories"][0]) == {
        "path", "relation", "eligibility", "reason_codes", "planned_action", "evidence",
    }
    assert result["repositories"][0]["planned_action"] == "fast_forward"
    assert result["repositories"][1]["planned_action"] == "none"
    assert result["repositories"][1]["evidence"] == {
        "head": None, "upstream": None, "ahead": None, "behind": None,
        "status_fingerprint": None,
    }
    assert [event for _, event in events] == ["decision_recorded", "decision_recorded"]


def test_plan_outcome_is_complete_for_ordinary_blocked_repositories(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(core, "discover_projects", lambda roots, max_depth=5: [repo])
    monkeypatch.setattr(core, "inspect_project", lambda path, fetch=False: {
        "path": str(repo), "relation": "ahead", "eligibility": "blocked",
        "reason_codes": ["local_ahead_of_upstream"], "head": "abc", "upstream": "origin/main",
        "ahead": 1, "behind": 0, "status_fingerprint": "clean",
    })
    monkeypatch.setattr(core, "record_event", lambda *args, **kwargs: None)

    result = core.plan_projects([str(repo)], fetch=False, max_depth=0)

    assert result["outcome"] == "complete"
    assert result["summary"] == {"total": 1, "planned": 0, "blocked": 1, "unknown": 0}


def test_plan_never_pulls_and_fetches_only_when_requested(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    real = core._run_git
    commands = []

    def spy(path, args, timeout_s=20.0, **kwargs):
        commands.append(tuple(args))
        return real(path, args, timeout_s=timeout_s, **kwargs)

    monkeypatch.setattr(core, "_run_git", spy)
    core.plan_projects([str(repo)], base_dir=str(tmp_path / "state-a"), fetch=False, max_depth=0)
    assert not any(command[:1] in {("fetch",), ("pull",)} for command in commands)

    commands.clear()
    core.plan_projects([str(repo)], base_dir=str(tmp_path / "state-b"), fetch=True, max_depth=0)
    assert any(command[:1] == ("fetch",) for command in commands)
    assert not any(command[:1] == ("pull",) for command in commands)


def test_plan_then_external_dirtying_does_not_authorize_apply(monkeypatch, tmp_path):
    seed, clone = _remote_clone(tmp_path)
    _push_remote_change(seed)
    plan = core.plan_projects([str(clone)], base_dir=str(tmp_path / "state"), fetch=True, max_depth=0)
    assert plan["repositories"][0]["planned_action"] == "fast_forward"
    (clone / "local.txt").write_text("preserve me\n", encoding="utf-8")
    real = core._run_git
    pulls = []

    def spy(path, args, timeout_s=20.0, **kwargs):
        if list(args)[:1] == ["pull"]:
            pulls.append(list(args))
        return real(path, args, timeout_s=timeout_s, **kwargs)

    monkeypatch.setattr(core, "_run_git", spy)
    result = core.sync_projects([str(clone)], base_dir=str(tmp_path / "state"), apply=True, fetch=False, max_depth=0)

    assert result["projects"][0]["blocked"] is True
    assert result["projects"][0]["applied"] is False
    assert pulls == []
    assert (clone / "local.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_cli_plan_json_and_human_output(tmp_path, capsys):
    repo = tmp_path / "repo"
    _init_repo(repo)

    rc = main(["--base-dir", str(tmp_path / "state-json"), "sync", str(repo), "--plan", "--no-fetch", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["schema"] == "version-drift/plan/1"
    assert payload["fetch_performed"] is False

    rc = main(["--base-dir", str(tmp_path / "state-human"), "sync", str(repo), "--plan", "--no-fetch"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert rc == 0
    assert any("planned" in line.lower() for line in lines)
    assert any("blocked" in line.lower() for line in lines)
    assert lines[-2:] == [AUTHORIZATION, "Nothing was changed."]


def test_cli_plan_apply_and_fetch_flags_are_mutually_exclusive(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)

    with pytest.raises(SystemExit) as conflict:
        main(["sync", str(repo), "--plan", "--apply"])
    assert conflict.value.code == 2

    with pytest.raises(SystemExit) as conflict:
        main(["sync", str(repo), "--fetch", "--no-fetch"])
    assert conflict.value.code == 2


def test_plan_rejects_negative_depth(tmp_path):
    with pytest.raises(ValueError):
        core.plan_projects([str(tmp_path)], max_depth=-1)
