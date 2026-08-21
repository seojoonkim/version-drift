from pathlib import Path
import subprocess

import pytest

import version_drift.core as core


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def _repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "a").write_text("a\n", encoding="utf-8")
    _git(path, "add", "a")
    _git(path, "commit", "-m", "initial")


def _assert_hard_blocked(report, reason: str) -> None:
    assert reason in report["reason_codes"]
    assert report["state"] == "protected"
    assert report["safe_to_update"] is False
    assert report["eligibility"] == "blocked"
    assert report["state"] != "behind_clean"


def test_detached_head_is_hard_blocked(tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)
    _git(repo, "checkout", "--detach")

    _assert_hard_blocked(core.inspect_project(str(repo)), "detached_head")


@pytest.mark.parametrize(
    "marker",
    ["MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG"],
)
def test_git_operation_markers_are_hard_blocked_and_accumulate(tmp_path, marker):
    repo = tmp_path / "repo"
    _repo(repo)
    _git(repo, "checkout", "--detach")
    marker_path = Path(_git(repo, "rev-parse", "--git-path", marker))
    if not marker_path.is_absolute():
        marker_path = repo / marker_path
    if marker.startswith("rebase-"):
        marker_path.mkdir(parents=True)
    else:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text("operation\n", encoding="utf-8")

    report = core.inspect_project(str(repo))
    _assert_hard_blocked(report, "operation_in_progress")
    assert "detached_head" in report["reason_codes"]


def test_shallow_repository_is_hard_blocked(tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)
    shallow = Path(_git(repo, "rev-parse", "--git-path", "shallow"))
    if not shallow.is_absolute():
        shallow = repo / shallow
    shallow.write_text(_git(repo, "rev-parse", "HEAD") + "\n", encoding="ascii")
    assert _git(repo, "rev-parse", "--is-shallow-repository") == "true"

    _assert_hard_blocked(core.inspect_project(str(repo)), "shallow_repository")


def test_linked_worktree_is_hard_blocked(tmp_path):
    main = tmp_path / "main"
    linked = tmp_path / "linked"
    _repo(main)
    _git(main, "worktree", "add", "-b", "linked-test", str(linked))

    _assert_hard_blocked(core.inspect_project(str(linked)), "linked_worktree")


def test_submodule_checkout_is_hard_blocked(tmp_path):
    child = tmp_path / "child"
    parent = tmp_path / "parent"
    _repo(child)
    _repo(parent)
    _git(parent, "-c", "protocol.file.allow=always", "submodule", "add", str(child), "vendor/child")
    _git(parent, "commit", "-am", "add submodule")

    _assert_hard_blocked(core.inspect_project(str(parent / "vendor/child")), "submodule_checkout")


def test_tracked_gitmodules_is_hard_blocked(tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)
    (repo / ".gitmodules").write_text("# tracked metadata\n", encoding="utf-8")
    _git(repo, "add", ".gitmodules")
    _git(repo, "commit", "-m", "track gitmodules")

    _assert_hard_blocked(core.inspect_project(str(repo)), "contains_submodules")


def test_malformed_required_metadata_is_unknown_and_never_eligible(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)
    real = core._run_git

    def malformed(path, args, timeout_s=20.0, **kwargs):
        if list(args) == ["rev-parse", "--is-shallow-repository"]:
            return core.GitResult(True, "perhaps")
        return real(path, args, timeout_s=timeout_s, **kwargs)

    monkeypatch.setattr(core, "_run_git", malformed)
    report = core.inspect_project(str(repo))

    assert "git_metadata_unreadable" in report["reason_codes"]
    assert report["state"] == "protected"
    assert report["safe_to_update"] is False
    assert report["eligibility"] == "unknown"


def test_ordinary_repository_is_not_topology_blocked(tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)

    report = core.inspect_project(str(repo))

    hardening = {
        "detached_head", "operation_in_progress", "shallow_repository", "linked_worktree",
        "submodule_checkout", "contains_submodules", "git_metadata_unreadable",
    }
    assert hardening.isdisjoint(report["reason_codes"])


def test_apply_never_pulls_a_hard_blocked_repository(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)
    _git(repo, "checkout", "--detach")
    real = core._run_git
    pulls = []

    def spy(path, args, timeout_s=20.0, **kwargs):
        if list(args)[:1] == ["pull"]:
            pulls.append(list(args))
        return real(path, args, timeout_s=timeout_s, **kwargs)

    monkeypatch.setattr(core, "_run_git", spy)
    result = core.sync_project(str(repo), base_dir=str(tmp_path / "state"), apply=True, fetch=False)

    assert result["blocked"] is True
    assert result["applied"] is False
    assert "detached_head" in result["before"]["reason_codes"]
    assert pulls == []
