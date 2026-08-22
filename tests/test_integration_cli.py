import json
import os
import subprocess
from pathlib import Path

import pytest

from version_drift.cli import main


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
        check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "tracked.txt").write_text("main\n", encoding="utf-8")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-qm", "main")
    git(path, "branch", "feature")
    return path


def invoke(capsys, state: Path, *argv: str):
    code = main(["--base-dir", str(state), *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def add_args(repo: Path, intent_id="intent-a", source="refs/heads/feature", dependencies=()):
    args = [
        "integrate", "intent", "add", str(repo),
        "--repository-id", "local-repo-1", "--intent-id", intent_id,
        "--agent-id", "agent-7", "--source", source,
        "--target", "HEAD", "--summary", "Ship the feature",
    ]
    for dependency in dependencies:
        args.extend(["--depends-on", dependency])
    return args


def git_snapshot(repo: Path):
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    index = git_dir / "index"
    refs = {
        str(path.relative_to(git_dir)): path.read_bytes()
        for root in (git_dir / "refs",) if root.exists()
        for path in root.rglob("*") if path.is_file()
    }
    packed = git_dir / "packed-refs"
    if packed.exists():
        refs["packed-refs"] = packed.read_bytes()
    worktree = {
        str(path.relative_to(repo)): path.read_bytes()
        for path in repo.rglob("*") if path.is_file() and git_dir not in path.parents
    }
    return {
        "head": (git_dir / "HEAD").read_bytes(),
        "refs": refs,
        "index": index.read_bytes(),
        "index_stat": index.stat()[:10],
        "worktree": worktree,
    }


def test_integrate_help_describes_coordination_only(capsys):
    with pytest.raises(SystemExit) as raised:
        main(["integrate", "--help"])
    output = capsys.readouterr().out
    assert raised.value.code == 0
    assert "coordination" in output.lower()
    assert "does not merge" in output.lower()


def test_add_pins_full_oids_normalizes_repo_and_prints_human_and_json(repo, tmp_path, capsys):
    state = tmp_path / "state"
    code, output, error = invoke(capsys, state, *add_args(repo))
    assert (code, error) == (0, "")
    assert "Created immutable integration intent intent-a" in output
    stored = json.loads((state / ".version-drift/integration-intents/intent-a.json").read_text())
    assert stored["repository_path"] == str(repo.resolve())
    assert stored["source_oid"] == git(repo, "rev-parse", "refs/heads/feature^{commit}")
    assert stored["target_oid"] == git(repo, "rev-parse", "HEAD^{commit}")
    assert stored["base_oid"] == stored["target_oid"]
    assert len(stored["source_oid"]) == 40

    args = add_args(repo, intent_id="intent-b", dependencies=("intent-a",)) + ["--json"]
    code, output, error = invoke(capsys, state, *args)
    payload = json.loads(output)
    assert (code, error) == (0, "")
    assert payload["intent_id"] == "intent-b"
    assert payload["dependency_intent_ids"] == ["intent-a"]


def test_add_changes_only_external_version_drift_state(repo, tmp_path, capsys):
    state = tmp_path / "external-state"
    (repo / "tracked.txt").write_text("working tree edit\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("preserve me\n", encoding="utf-8")
    before = git_snapshot(repo)

    code, _, error = invoke(capsys, state, *add_args(repo))

    assert (code, error) == (0, "")
    assert (state / ".version-drift/integration-intents/intent-a.json").is_file()
    assert git_snapshot(repo) == before
    assert before["refs"] and before["index"] and before["worktree"]
    assert before["worktree"]["tracked.txt"] == b"working tree edit\n"
    assert before["worktree"]["untracked.txt"] == b"preserve me\n"


def test_duplicate_add_is_immutable_and_invalid_or_missing_refs_are_usage_errors(repo, tmp_path, capsys):
    state = tmp_path / "state"
    assert invoke(capsys, state, *add_args(repo))[0] == 0
    path = state / ".version-drift/integration-intents/intent-a.json"
    before = path.read_bytes()
    code, _, error = invoke(capsys, state, *add_args(repo))
    assert code == 1
    assert "already exists" in error
    assert path.read_bytes() == before

    missing = add_args(repo, intent_id="missing", source="refs/heads/no-such-ref")
    code, _, error = invoke(capsys, state, *missing)
    assert code == 2
    assert "cannot resolve source ref" in error
    assert not (path.parent / "missing.json").exists()

    code, _, error = invoke(capsys, state, *add_args(tmp_path / "not-a-repo", intent_id="invalid"))
    assert code == 2
    assert "repository" in error.lower()


@pytest.mark.parametrize("kind", ["directory-symlink", "dangling-symlink", "file"])
def test_add_rejects_unsafe_final_store_path_without_writing_or_changing_git(
        kind, repo, tmp_path, capsys):
    state = tmp_path / "state"
    store_path = state / ".version-drift" / "integration-intents"
    store_path.parent.mkdir(parents=True)
    target = tmp_path / "intent-target"
    if kind == "file":
        store_path.write_text("not a directory", encoding="utf-8")
    else:
        if kind == "directory-symlink":
            target.mkdir()
        try:
            store_path.symlink_to(target, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
    before_git = git_snapshot(repo)

    code, output, error = invoke(capsys, state, *add_args(repo))

    assert code != 0
    assert output == ""
    assert "intent store" in error.lower() or "integration-intents" in error
    assert git_snapshot(repo) == before_git
    if kind == "directory-symlink":
        assert list(target.iterdir()) == []
    elif kind == "dangling-symlink":
        assert not target.exists()
    else:
        assert store_path.read_text(encoding="utf-8") == "not a directory"


def test_list_human_and_json_are_deterministic_and_malformed_store_fails_closed(repo, tmp_path, capsys):
    state = tmp_path / "state"
    assert invoke(capsys, state, *add_args(repo, "z"))[0] == 0
    assert invoke(capsys, state, *add_args(repo, "a", source="HEAD"))[0] == 0
    command = ["integrate", "intent", "list", str(repo), "--repository-id", "local-repo-1"]
    code, human, error = invoke(capsys, state, *command)
    assert (code, error) == (0, "")
    assert human.index("a ") < human.index("z ")
    assert "Nothing in Git was changed." in human

    code, first, error = invoke(capsys, state, *command, "--json")
    code2, second, _ = invoke(capsys, state, *command, "--json")
    payload = json.loads(first)
    assert (code, code2, error, first) == (0, 0, "", second)
    assert [item["intent_id"] for item in payload] == ["a", "z"]

    (path := state / ".version-drift/integration-intents/broken.json").write_text("{broken", encoding="utf-8")
    before = path.read_bytes()
    code, output, error = invoke(capsys, state, *command, "--json")
    assert code == 3
    assert output == ""
    assert "malformed" in error.lower() or "invalid" in error.lower()
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["file", "directory-symlink", "dangling-symlink"])
def test_list_and_board_fail_closed_for_invalid_store_paths(kind, repo, tmp_path, capsys):
    state = tmp_path / "state"
    store_path = state / ".version-drift" / "integration-intents"
    store_path.parent.mkdir(parents=True)
    if kind == "file":
        store_path.write_text("not a directory", encoding="utf-8")
    else:
        target = tmp_path / "intent-target"
        if kind == "directory-symlink":
            target.mkdir()
        try:
            store_path.symlink_to(target, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"symlinks unavailable: {exc}")

    list_command = [
        "integrate", "intent", "list", str(repo),
        "--repository-id", "local-repo-1", "--json",
    ]
    code, output, error = invoke(capsys, state, *list_command)
    assert code == 3
    assert output == ""
    assert "malformed or unreadable store" in error

    board_command = [
        "integrate", "board", str(repo), "--repository-id", "local-repo-1",
        "--target", "HEAD", "--json",
    ]
    code, output, error = invoke(capsys, state, *board_command)
    assert code == 3
    assert error == ""
    payload = json.loads(output)
    assert payload["status"] == "UNKNOWN"
    assert payload["reason"] == "MALFORMED_INTENT_STORE"


def test_board_ready_human_json_order_and_exit_codes(repo, tmp_path, capsys):
    state = tmp_path / "state"
    assert invoke(capsys, state, *add_args(repo, "z"))[0] == 0
    git(repo, "branch", "other")
    assert invoke(capsys, state, *add_args(repo, "a", source="refs/heads/other"))[0] == 0
    command = [
        "integrate", "board", str(repo), "--repository-id", "local-repo-1",
        "--target", "HEAD",
    ]
    code, human, error = invoke(capsys, state, *command)
    assert (code, error) == (0, "")
    assert "Integration board: READY" in human
    assert "1. a: READY" in human and "2. z: READY" in human
    assert "Nothing in Git was changed." in human

    code, first, _ = invoke(capsys, state, *command, "--json")
    code2, second, _ = invoke(capsys, state, *command, "--json")
    payload = json.loads(first)
    assert (code, code2, first) == (0, 0, second)
    assert payload["schema"] == "version-drift/integration-board/1"
    assert payload["status"] == "READY"
    assert [item["intent_id"] for item in payload["items"]] == ["a", "z"]

    git(repo, "branch", "-f", "feature", "HEAD~0")
    # Advance feature without checking it out, making the pinned source stale.
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    new_oid = git(repo, "commit-tree", tree, "-p", "HEAD", "-m", "advance")
    git(repo, "update-ref", "refs/heads/feature", new_oid)
    code, output, _ = invoke(capsys, state, *command, "--json")
    assert code == 1
    assert json.loads(output)["status"] == "STALE"


def test_board_blocked_unknown_and_malformed_exit_contract(repo, tmp_path, capsys):
    state = tmp_path / "state"
    missing_dep = add_args(repo, "blocked", dependencies=("absent",))
    assert invoke(capsys, state, *missing_dep)[0] == 0
    command = ["integrate", "board", str(repo), "--repository-id", "local-repo-1", "--target", "HEAD", "--json"]
    code, output, _ = invoke(capsys, state, *command)
    assert code == 1
    assert json.loads(output)["status"] == "BLOCKED"
    assert json.loads(output)["items"][0]["reason"] == "MISSING_DEPENDENCY"

    git(repo, "branch", "-D", "feature")
    code, output, _ = invoke(capsys, state, *command)
    assert code == 3
    assert json.loads(output)["status"] == "UNKNOWN"

    broken = state / ".version-drift/integration-intents/broken.json"
    broken.write_text("not json", encoding="utf-8")
    code, output, _ = invoke(capsys, state, *command)
    assert code == 3
    payload = json.loads(output)
    assert payload["status"] == "UNKNOWN"
    assert payload["reason"] == "MALFORMED_INTENT_STORE"


def test_list_and_board_do_not_mutate_nonvacuous_git_or_state(repo, tmp_path, capsys):
    state = tmp_path / "state"
    assert invoke(capsys, state, *add_args(repo))[0] == 0
    (repo / "untracked.txt").write_text("preserve me\n", encoding="utf-8")
    intent_file = state / ".version-drift/integration-intents/intent-a.json"
    before_git = git_snapshot(repo)
    before_state = (intent_file.read_bytes(), intent_file.stat()[:10])

    list_command = ["integrate", "intent", "list", str(repo), "--repository-id", "local-repo-1", "--json"]
    board_command = ["integrate", "board", str(repo), "--repository-id", "local-repo-1", "--target", "HEAD", "--json"]
    assert invoke(capsys, state, *list_command)[0] == 0
    assert invoke(capsys, state, *board_command)[0] == 0

    assert git_snapshot(repo) == before_git
    assert (intent_file.read_bytes(), intent_file.stat()[:10]) == before_state
    assert before_git["refs"] and before_git["index"] and before_git["worktree"]


def test_board_reports_missing_target_as_structured_unknown(repo, tmp_path, capsys):
    state = tmp_path / "state"
    command = [
        "integrate", "board", str(repo), "--repository-id", "local-repo-1",
        "--target", "missing", "--json",
    ]
    code, output, error = invoke(capsys, state, *command)
    assert code == 3
    assert error == ""
    payload = json.loads(output)
    assert payload["status"] == "UNKNOWN"
    assert payload["reason"] == "TARGET_REF_UNOBSERVABLE"
    assert payload["items"] == []
