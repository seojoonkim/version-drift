import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from version_drift.integrate import (
    BOARD_SCHEMA,
    INTENT_SCHEMA,
    BoardStatus,
    IntegrationBoard,
    IntegrationIntent,
    IntegrationIntentStore,
    ReasonCode,
)


def git(repo, *args, check=True):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=check,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "tracked.txt").write_text("main\n", encoding="utf-8")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-qm", "main")
    main = git(path, "rev-parse", "HEAD").stdout.strip()
    tree = git(path, "rev-parse", "HEAD^{tree}").stdout.strip()
    feature = git(path, "commit-tree", tree, "-p", main, "-m", "feature").stdout.strip()
    other = git(path, "commit-tree", tree, "-p", main, "-m", "other").stdout.strip()
    git(path, "update-ref", "refs/heads/feature", feature)
    git(path, "update-ref", "refs/heads/other", other)
    return path


def oid(repo, ref):
    return git(repo, "rev-parse", "--verify", ref + "^{commit}").stdout.strip()


def make_intent(repo, intent_id="a", source_ref="refs/heads/feature", target_ref=None,
                dependencies=(), repository_id="repo-1", repository_path=None,
                source_oid=None, target_oid=None):
    target_ref = target_ref or git(repo, "symbolic-ref", "HEAD").stdout.strip()
    return IntegrationIntent(
        schema=INTENT_SCHEMA,
        intent_id=intent_id,
        agent_id="agent",
        repository_path=repository_path or str(repo.resolve()),
        repository_id=repository_id,
        source_ref=source_ref,
        target_ref=target_ref,
        base_oid=oid(repo, target_ref),
        source_oid=source_oid or oid(repo, source_ref),
        target_oid=target_oid or oid(repo, target_ref),
        summary="ready",
        dependency_intent_ids=tuple(dependencies),
        created_at="2026-08-22T10:20:30Z",
    )


def board(repo):
    return IntegrationBoard(repo, "repo-1", git(repo, "symbolic-ref", "HEAD").stdout.strip())


def by_id(result):
    return {item.intent_id: item for item in result.items}


def test_ready_result_is_versioned_structured_and_deterministic(repo):
    git(repo, "update-ref", "refs/heads/z", oid(repo, "refs/heads/other"))
    intents = [
        make_intent(repo, "z", source_ref="refs/heads/z"),
        make_intent(repo, "b", dependencies=("a",)),
        make_intent(repo, "a", source_ref="refs/heads/other"),
    ]

    result = board(repo).inspect(intents)

    assert result.schema == BOARD_SCHEMA
    assert result.status is BoardStatus.READY
    assert [item.intent_id for item in result.items] == ["a", "b", "z"]
    assert all(item.status is BoardStatus.READY for item in result.items)
    assert result.to_dict() == board(repo).inspect(reversed(intents)).to_dict()
    assert result.to_dict()["schema"] == "version-drift/integration-board/1"


def test_stale_source_and_target_are_reported_separately(repo):
    wrong = "0" * 40
    result = board(repo).inspect([
        make_intent(repo, "source", source_oid=wrong),
        make_intent(repo, "target", source_ref="refs/heads/other", target_oid=wrong),
    ])

    items = by_id(result)
    assert (items["source"].status, items["source"].reason) == (BoardStatus.STALE, ReasonCode.SOURCE_OID_CHANGED)
    assert (items["target"].status, items["target"].reason) == (BoardStatus.STALE, ReasonCode.TARGET_OID_CHANGED)


def test_missing_ref_and_malformed_git_output_fail_closed(repo, monkeypatch):
    missing = make_intent(repo, "missing", source_ref="refs/heads/missing", source_oid="1" * 40)
    result = board(repo).inspect([missing])
    assert (result.items[0].status, result.items[0].reason) == (BoardStatus.UNKNOWN, ReasonCode.SOURCE_REF_UNOBSERVABLE)

    import version_drift.integrate.board as module
    real_run = module.subprocess.run
    valid = make_intent(repo)

    def malformed(*args, **kwargs):
        completed = real_run(*args, **kwargs)
        if "rev-parse" in args[0]:
            return subprocess.CompletedProcess(
                completed.args, completed.returncode, "not-an-oid\n", completed.stderr)
        return completed

    monkeypatch.setattr(module.subprocess, "run", malformed)
    result = board(repo).inspect([valid])
    assert (result.items[0].status, result.items[0].reason) == (BoardStatus.UNKNOWN, ReasonCode.SOURCE_REF_UNOBSERVABLE)


def test_duplicate_source_ownership_blocks_all_claimants(repo):
    result = board(repo).inspect([make_intent(repo, "b"), make_intent(repo, "a")])
    assert [(x.intent_id, x.status, x.reason) for x in result.items] == [
        ("a", BoardStatus.BLOCKED, ReasonCode.DUPLICATE_SOURCE_REF),
        ("b", BoardStatus.BLOCKED, ReasonCode.DUPLICATE_SOURCE_REF),
    ]


def test_missing_cross_board_and_nonready_dependencies_block(repo, tmp_path):
    other_path = tmp_path / "elsewhere"
    intents = [
        make_intent(repo, "missing", dependencies=("absent",)),
        make_intent(repo, "foreign", repository_id="other"),
        make_intent(repo, "uses-foreign", source_ref="refs/heads/other", dependencies=("foreign",)),
        make_intent(repo, "stale", source_ref="refs/heads/other", source_oid="0" * 40),
        make_intent(repo, "uses-stale", dependencies=("stale",)),
        make_intent(repo, "path", repository_path=str(other_path.resolve())),
        make_intent(repo, "target", target_ref="refs/heads/other"),
    ]
    result = board(repo).inspect(intents)
    items = by_id(result)
    assert items["missing"].reason is ReasonCode.MISSING_DEPENDENCY
    assert items["foreign"].reason is ReasonCode.REPOSITORY_MISMATCH
    assert items["path"].reason is ReasonCode.REPOSITORY_MISMATCH
    assert items["target"].reason is ReasonCode.TARGET_MISMATCH
    assert items["uses-foreign"].reason is ReasonCode.DEPENDENCY_OUTSIDE_BOARD
    assert items["uses-stale"].reason is ReasonCode.DEPENDENCY_NOT_READY


def test_cycles_are_blocked_without_harming_acyclic_order(repo):
    intents = [
        make_intent(repo, "cycle-b", dependencies=("cycle-a",)),
        make_intent(repo, "root", source_ref="refs/heads/other"),
        make_intent(repo, "cycle-a", source_ref="refs/heads/other", dependencies=("cycle-b",)),
        make_intent(repo, "after", dependencies=("root",)),
    ]
    # Duplicate refs are irrelevant here: give cycle members distinct synthetic refs.
    git(repo, "update-ref", "refs/heads/cycle-a", oid(repo, "refs/heads/feature"))
    git(repo, "update-ref", "refs/heads/cycle-b", oid(repo, "refs/heads/other"))
    intents[0] = replace(
        intents[0], source_ref="refs/heads/cycle-b",
        source_oid=oid(repo, "refs/heads/cycle-b"))
    intents[2] = replace(
        intents[2], source_ref="refs/heads/cycle-a",
        source_oid=oid(repo, "refs/heads/cycle-a"))

    result = board(repo).inspect(intents)
    assert [x.intent_id for x in result.items if x.status is BoardStatus.READY] == ["root", "after"]
    assert {x.reason for x in result.items if x.intent_id.startswith("cycle-")} == {ReasonCode.DEPENDENCY_CYCLE}


def snapshot(repo):
    git_dir = repo / ".git"
    index = git_dir / "index"
    worktree = {
        str(path.relative_to(repo)): path.read_bytes()
        for path in repo.rglob("*") if path.is_file() and git_dir not in path.parents
    }
    refs = {
        str(path.relative_to(git_dir)): path.read_bytes()
        for root in (git_dir / "refs",) for path in root.rglob("*") if path.is_file()
    }
    packed = git_dir / "packed-refs"
    if packed.exists():
        refs["packed-refs"] = packed.read_bytes()
    return {
        "head": (git_dir / "HEAD").read_bytes(),
        "refs": refs,
        "index_bytes": index.read_bytes() if index.exists() else None,
        "index_stat": index.stat()[:10] if index.exists() else None,
        "worktree": worktree,
    }


def test_board_is_strictly_read_only_for_head_refs_index_and_worktree(repo):
    (repo / "untracked.txt").write_text("keep me\n", encoding="utf-8")
    before = snapshot(repo)

    result = board(repo).inspect([make_intent(repo)])

    after = snapshot(repo)
    assert result.items[0].status is BoardStatus.READY
    assert after == before


def test_store_loading_and_malformed_persisted_intent_fail_closed(repo, tmp_path):
    store = IntegrationIntentStore(base_dir=tmp_path / "state")
    store.create(make_intent(repo))
    assert board(repo).inspect_store(store).items[0].status is BoardStatus.READY

    (store.directory / "broken.json").write_text("{ definitely not json", encoding="utf-8")
    result = board(repo).inspect_store(store)
    assert result.status is BoardStatus.UNKNOWN
    assert result.items == ()
    assert result.reason is ReasonCode.MALFORMED_INTENT_STORE


def test_git_invocation_is_lockless_and_uses_only_read_commands(repo, monkeypatch):
    import version_drift.integrate.board as module
    evaluator = board(repo)
    value = make_intent(repo)
    calls = []
    real_run = module.subprocess.run

    def record(*args, **kwargs):
        calls.append((args[0], kwargs["env"]))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", record)
    evaluator.inspect([value])

    assert calls
    assert all(env["GIT_OPTIONAL_LOCKS"] == "0" for _, env in calls)
    assert {command[1] for command, _ in calls} == {"rev-parse"}
    forbidden = {"fetch", "merge", "rebase", "stash", "reset", "update-ref", "checkout", "switch"}
    assert not any(forbidden.intersection(command) for command, _ in calls)
