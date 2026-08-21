import json
import subprocess
from pathlib import Path

import pytest

import version_drift.cli as cli
import version_drift.core as core


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_public_discovery_never_returns_directory_symlink_repo(tmp_path):
    target = tmp_path / "target-repo"
    _init_repo(target)
    link = tmp_path / "linked-repo"
    link.symlink_to(target, target_is_directory=True)

    assert core.discover_projects([str(link)]) == []


@pytest.mark.parametrize("operation", ["scan", "plan"])
def test_explicit_symlink_scope_fails_closed_without_inspecting_target(
    monkeypatch, tmp_path, operation
):
    target = tmp_path / "outside" / "target-repo"
    _init_repo(target)
    boundary = tmp_path / "boundary"
    boundary.mkdir()
    link = boundary / "linked-repo"
    link.symlink_to(target, target_is_directory=True)
    inspected = []

    def inspect(path, **kwargs):
        inspected.append(path)
        raise AssertionError("symlink target must not be inspected")

    monkeypatch.setattr(core, "inspect_project", inspect)
    state = tmp_path / (operation + "-state")
    if operation == "scan":
        result = core.scan_projects([str(link)], base_dir=str(state), fetch=False)
        observations = result["projects"]
        reasons = observations[0]["reason_codes"]
    else:
        result = core.plan_projects([str(link)], base_dir=str(state), fetch=False)
        observations = result["repositories"]
        reasons = observations[0]["reason_codes"]

    assert inspected == []
    assert result["outcome"] == "failed"
    assert len(observations) == 1
    assert observations[0]["path"] == str(link.absolute())
    assert "scope_inspection_failed" in reasons
    assert str(target) not in str(result)


@pytest.mark.parametrize("apply", [False, True])
def test_sync_through_directory_symlink_fails_and_never_pulls(
    monkeypatch, tmp_path, apply
):
    target = tmp_path / "outside" / "target-repo"
    _init_repo(target)
    boundary = tmp_path / "boundary"
    boundary.mkdir()
    link = boundary / "linked-repo"
    link.symlink_to(target, target_is_directory=True)
    pulls = []
    real_run_git = core._run_git

    def spy(repo, args, **kwargs):
        if list(args)[:1] == ["pull"]:
            pulls.append((repo, args))
        return real_run_git(repo, args, **kwargs)

    monkeypatch.setattr(core, "_run_git", spy)
    result = core.sync_projects(
        [str(link)], base_dir=str(tmp_path / "state"), apply=apply, fetch=False
    )

    assert result["outcome"] == "failed"
    assert result["summary"]["failed"] == 1
    assert result["projects"][0]["blocked"] is True
    assert result["projects"][0]["reason"] == "scope_inspection_failed"
    assert result["projects"][0]["before"]["path"] == str(link.absolute())
    assert str(target) not in str(result)
    assert pulls == []


@pytest.mark.parametrize("apply_flag", [[], ["--apply"]])
def test_sync_cli_through_directory_symlink_is_operationally_nonzero(
    monkeypatch, tmp_path, capsys, apply_flag
):
    target = tmp_path / "outside" / "target-repo"
    _init_repo(target)
    link = tmp_path / "linked-repo"
    link.symlink_to(target, target_is_directory=True)
    pulls = []
    real_run_git = core._run_git

    def spy(repo, args, **kwargs):
        if list(args)[:1] == ["pull"]:
            pulls.append((repo, args))
        return real_run_git(repo, args, **kwargs)

    monkeypatch.setattr(core, "_run_git", spy)
    code = cli.main([
        "--base-dir", str(tmp_path / "state"), "sync", str(link),
        "--no-fetch", "--json", *apply_flag,
    ])
    payload = json.loads(capsys.readouterr().out)

    assert code == 3
    assert payload["outcome"] == "failed"
    assert payload["projects"][0]["reason"] == "scope_inspection_failed"
    assert pulls == []


def test_nested_symlink_is_skipped_without_changing_ordinary_root_semantics(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "outside" / "target-repo"
    _init_repo(target)
    (root / "linked-repo").symlink_to(target, target_is_directory=True)

    assert core.discover_projects([str(root)]) == []
    result = core.scan_projects(
        [str(root)], base_dir=str(tmp_path / "state"), fetch=False
    )
    assert result["outcome"] == "complete"
    assert result["summary"]["total"] == 0
    assert str(target) not in str(result)


def test_symlink_in_parent_component_is_failed_closed(tmp_path):
    outside = tmp_path / "outside"
    target = outside / "nested" / "repo"
    _init_repo(target)
    boundary = tmp_path / "boundary"
    boundary.mkdir()
    (boundary / "escape").symlink_to(outside, target_is_directory=True)
    supplied = boundary / "escape" / "nested" / "repo"

    assert core.discover_projects([str(supplied)]) == []
    result = core.plan_projects(
        [str(supplied)], base_dir=str(tmp_path / "state"), fetch=False
    )
    assert result["outcome"] == "failed"
    assert result["repositories"][0]["path"] == str(supplied.absolute())
    assert result["repositories"][0]["reason_codes"] == ["scope_inspection_failed"]
    assert str(target) not in str(result)


def test_direct_real_repository_remains_accepted(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)

    assert core.discover_projects([str(repo)]) == [repo.resolve()]
    result = core.scan_projects(
        [str(repo)], base_dir=str(tmp_path / "state"), fetch=False
    )
    assert result["summary"]["total"] == 1
    assert result["projects"][0]["path"] == str(repo.resolve())
    assert "scope_inspection_failed" not in result["projects"][0]["reason_codes"]


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("symlink_kind", ["directory", "parent_component"])
def test_direct_sync_project_rejects_any_symlink_component_before_side_effects(
    monkeypatch, tmp_path, apply, symlink_kind
):
    target = tmp_path / "outside" / "nested" / "target-repo"
    _init_repo(target)
    boundary = tmp_path / "boundary"
    boundary.mkdir()
    if symlink_kind == "directory":
        supplied = boundary / "linked-repo"
        supplied.symlink_to(target, target_is_directory=True)
    else:
        (boundary / "escape").symlink_to(tmp_path / "outside", target_is_directory=True)
        supplied = boundary / "escape" / "nested" / "target-repo"

    def forbidden(*args, **kwargs):
        raise AssertionError("a rejected path must not be inspected, logged, fetched, or pulled")

    monkeypatch.setattr(core, "inspect_project", forbidden)
    monkeypatch.setattr(core, "record_event", forbidden)
    monkeypatch.setattr(core, "_run_git", forbidden)

    result = core.sync_project(
        str(supplied), base_dir=str(tmp_path / "state"), apply=apply, fetch=True
    )

    assert result["blocked"] is True
    assert result["applied"] is False
    assert result["after"] is None
    assert result["reason"] == "scope_inspection_failed"
    assert result["before"]["path"] == str(supplied.absolute())
    assert result["before"]["reason_codes"] == ["scope_inspection_failed"]
    assert str(target) not in str(result)


def test_direct_sync_project_preserves_real_repository_behavior(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)

    result = core.sync_project(
        str(repo), base_dir=str(tmp_path / "state"), apply=False, fetch=False
    )

    assert result["before"]["path"] == str(repo.absolute())
    assert result["before"]["is_git"] is True
    assert result["blocked"] is True
    assert result["applied"] is False
    assert result["reason"] == "only_clean_fast_forward_sync_is_allowed"
    assert result["before"]["reason_codes"] != ["scope_inspection_failed"]
    history = core.history(base_dir=str(tmp_path / "state"))
    assert history["counts"]["returned"] == 1
    assert history["events"][0]["event"] == "sync_blocked"
    assert history["events"][0]["path"] == str(repo.absolute())
