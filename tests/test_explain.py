import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

import version_drift.cli as cli
from version_drift.cli import main
from version_drift.core import is_safe_fast_forward
from version_drift.explain import ACTIONS, EXPLAIN_SCHEMA, build_explanation, explain_reports


def _report(path: Path, state: str, **values):
    report = {
        "schema": "version-drift/1",
        "checked_at": "changes-every-run",
        "path": str(path.resolve()),
        "state": state,
        "ok": state == "synced",
        "safe_to_update": state == "behind_clean",
        "branch": "main",
        "upstream": "origin/main",
        "ahead": 0,
        "behind": 0,
        "reasons": [],
    }
    report.update(values)
    return report


@pytest.mark.parametrize(
    ("state", "reasons", "expected_key", "eligible"),
    [
        ("synced", [], "synced", False),
        ("behind_clean", ["local_behind_upstream"], "behind_clean", True),
        ("ahead", ["local_ahead_of_upstream"], "ahead", False),
        ("diverged", ["diverged_from_upstream"], "diverged", False),
        ("protected", ["dirty_worktree"], "dirty_worktree", False),
        ("protected", ["missing_upstream"], "missing_upstream", False),
        ("protected", ["fetch_failed"], "fetch_failed", False),
        ("protected", ["ahead_behind_unavailable"], "protected", False),
        ("invalid_head", ["invalid_head"], "invalid", False),
        ("not_git", ["not_git"], "invalid", False),
        ("missing", ["path_missing"], "invalid", False),
    ],
)
def test_explanation_uses_existing_state_and_reason_contract(tmp_path, state, reasons, expected_key, eligible):
    report = _report(tmp_path / state, state, reasons=reasons)

    item = build_explanation(report)

    assert item["state"] == state
    assert item["explanation_key"] == expected_key
    assert item["sync_eligible"] is eligible
    assert item["why_it_matters"]
    assert [action["description"] for action in item["safe_actions"]] == [
        action["description"] for action in ACTIONS[expected_key]["safe_actions"]
    ]
    assert "checked_at" not in item


def test_safe_fast_forward_predicate_is_the_shared_sync_contract(tmp_path):
    eligible = _report(tmp_path / "eligible", "behind_clean", safe_to_update=True)
    wrong_state = _report(tmp_path / "wrong-state", "ahead", safe_to_update=True)
    unsafe = _report(tmp_path / "unsafe", "behind_clean", safe_to_update=False)

    assert is_safe_fast_forward(eligible) is True
    assert is_safe_fast_forward(wrong_state) is False
    assert is_safe_fast_forward(unsafe) is False
    assert build_explanation(eligible)["sync_eligible"] is True
    assert build_explanation(wrong_state)["sync_eligible"] is False
    assert build_explanation(unsafe)["sync_eligible"] is False


def test_explain_json_is_deterministic_sorted_and_timestamp_free(tmp_path):
    reports = [
        _report(tmp_path / "z", "ahead", ahead=2, reasons=["local_ahead_of_upstream"]),
        _report(tmp_path / "a", "synced"),
    ]

    first = explain_reports(reports)
    second = explain_reports(list(reversed(reports)))

    assert first == second
    assert first["schema"] == EXPLAIN_SCHEMA
    assert [item["path"] for item in first["repositories"]] == sorted(item["path"] for item in first["repositories"])
    assert first["summary"] == {
        "total": 2,
        "by_state": {"ahead": 1, "synced": 1},
        "sync_eligible": 0,
    }
    assert "checked_at" not in json.dumps(first)


def test_advice_never_contains_destructive_git_commands():
    text = json.dumps(ACTIONS, sort_keys=True).lower()
    for banned in ("git reset", "git clean", "git stash", "git rebase", "git merge", "--force", "push -f"):
        assert banned not in text


def test_cli_explain_explicit_paths_bypass_config_and_never_fetch_or_record(monkeypatch, tmp_path, capsys):
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    calls = []

    def fake_inspect(path, fetch=False, optional_locks=True):
        calls.append((str(Path(path).resolve()), fetch, optional_locks))
        return _report(Path(path), "synced")

    monkeypatch.setattr(cli, "inspect_project", fake_inspect)
    monkeypatch.setattr(cli, "resolve_roots", lambda roots: pytest.fail("explicit paths must bypass configured roots"))
    monkeypatch.setattr(cli, "record_event", lambda *a, **k: pytest.fail("explain must not record events"))

    rc = main(["explain", str(two), str(one), str(one), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert calls == [
        (str(one.resolve()), False, False),
        (str(two.resolve()), False, False),
    ]
    assert [item["path"] for item in payload["repositories"]] == [str(one.resolve()), str(two.resolve())]


def test_cli_explain_without_paths_discovers_configured_roots_read_only(monkeypatch, tmp_path, capsys):
    root = tmp_path / "root"
    repo = root / "repo"
    repo.mkdir(parents=True)
    seen = {}

    monkeypatch.setattr(cli, "resolve_roots", lambda paths: [str(root.resolve())])

    def fake_discover(roots, max_depth=5):
        seen["discover"] = (list(roots), max_depth)
        return [repo]

    def fake_inspect(path, fetch=False, optional_locks=True):
        seen["inspect"] = (str(Path(path).resolve()), fetch, optional_locks)
        return _report(Path(path), "protected", reasons=["missing_upstream"], upstream="")

    monkeypatch.setattr(cli, "discover_projects", fake_discover)
    monkeypatch.setattr(cli, "inspect_project", fake_inspect)

    rc = main(["explain", "--max-depth", "3", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert seen == {
        "discover": ([str(root.resolve())], 3),
        "inspect": (str(repo.resolve()), False, False),
    }
    assert payload["repositories"][0]["explanation_key"] == "missing_upstream"


def test_cli_explain_human_output_is_stable_and_actionable(monkeypatch, tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        cli,
        "inspect_project",
        lambda path, fetch=False, optional_locks=True: _report(
            Path(path),
            "behind_clean",
            behind=3,
            reasons=["local_behind_upstream"],
        ),
    )

    assert main(["explain", str(repo)]) == 0
    first = capsys.readouterr().out
    assert main(["explain", str(repo)]) == 0
    second = capsys.readouterr().out

    assert first == second
    assert "VersionDrift explain: 1 repository" in first
    assert str(repo.resolve()) in first
    assert "3 commits behind" in first
    assert "version-drift sync" in first
    assert "Nothing was changed." in first


def test_cli_explain_existing_non_repository_is_an_explained_entry(monkeypatch, tmp_path, capsys):
    directory = tmp_path / "plain"
    directory.mkdir()
    monkeypatch.setattr(
        cli,
        "inspect_project",
        lambda path, fetch=False, optional_locks=True: _report(
            Path(path), "not_git", branch="", upstream="", reasons=["not_git"]
        ),
    )

    rc = main(["explain", str(directory), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["repositories"][0]["state"] == "not_git"
    assert payload["repositories"][0]["branch"] is None
    assert payload["repositories"][0]["upstream"] is None


def test_cli_explain_invalid_saved_config_fails_closed(monkeypatch, capsys):
    monkeypatch.setattr(cli, "resolve_roots", lambda paths: (_ for _ in ()).throw(ValueError("bad config")))

    rc = main(["explain", "--json"])

    assert rc == 1
    assert "version-drift explain: bad config" in capsys.readouterr().err


def test_cli_explain_missing_explicit_path_is_explained_without_mutation(tmp_path, capsys):
    missing = tmp_path / "missing"

    rc = main(["explain", str(missing), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["repositories"][0]["state"] == "missing"
    assert payload["repositories"][0]["explanation_key"] == "invalid"
    assert not missing.exists()


def test_cli_explain_rejects_negative_max_depth(capsys):
    rc = main(["explain", "--max-depth", "-1"])

    assert rc == 2
    assert "max depth must be non-negative" in capsys.readouterr().err


def test_explain_action_commands_shell_quote_the_current_path(tmp_path):
    path = tmp_path / "repo with $HOME and `tick`"
    item = build_explanation(
        _report(path, "behind_clean", behind=1, reasons=["local_behind_upstream"])
    )

    commands = [action.get("command") for action in item["safe_actions"] if action.get("command")]
    assert commands == ["version-drift sync --apply -- " + shlex.quote(str(path.resolve()))]
    assert shlex.split(commands[0])[-1] == str(path.resolve())
    assert all("<" not in command and ">" not in command for command in commands)


def test_explain_does_not_claim_dirty_and_missing_upstream_is_syncable(tmp_path):
    report = _report(
        tmp_path / "repo",
        "protected",
        safe_to_update=False,
        reasons=["dirty_worktree", "missing_upstream"],
    )

    item = build_explanation(report)

    assert item["explanation_key"] == "dirty_worktree"
    assert item["sync_eligible"] is False
    assert all("sync --apply" not in (action.get("command") or "") for action in item["safe_actions"])


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def test_real_explain_is_read_only_for_git_and_version_drift_state(tmp_path, capsys):
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    (repo / "local.txt").write_text("preserve me\n", encoding="utf-8")
    event_path = state / ".version-drift" / "events.jsonl"
    snapshot_path = state / ".version-drift" / "inbox_snapshot.json"
    event_path.parent.mkdir(parents=True)
    event_path.write_text("sentinel event\n", encoding="utf-8")
    snapshot_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    index_path = Path(_git(repo, "rev-parse", "--git-path", "index"))
    if not index_path.is_absolute():
        index_path = repo / index_path
    tracked = repo / "tracked.txt"
    original_stat = tracked.stat()
    os.utime(tracked, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000_000))
    before = {
        "head": _git(repo, "rev-parse", "HEAD"),
        "status": _git(repo, "status", "--porcelain=v1", "-uall"),
        "refs": _git(repo, "show-ref", "--head"),
        "index_bytes": index_path.read_bytes(),
        "index_mtime_ns": index_path.stat().st_mtime_ns,
        "event": event_path.read_bytes(),
        "snapshot": snapshot_path.read_bytes(),
    }
    os.utime(tracked, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 2_000_000_000))

    rc = main(["--base-dir", str(state), "explain", str(repo), "--json"])
    payload = json.loads(capsys.readouterr().out)

    after = {
        "index_bytes": index_path.read_bytes(),
        "index_mtime_ns": index_path.stat().st_mtime_ns,
        "head": _git(repo, "rev-parse", "HEAD"),
        "status": _git(repo, "status", "--porcelain=v1", "-uall"),
        "refs": _git(repo, "show-ref", "--head"),
        "event": event_path.read_bytes(),
        "snapshot": snapshot_path.read_bytes(),
    }
    assert rc == 0
    assert payload["repositories"][0]["explanation_key"] == "dirty_worktree"
    assert before == after
