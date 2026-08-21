import json
import os
import subprocess
from pathlib import Path

import pytest

import version_drift.config as config
import version_drift.inbox as inbox
from version_drift.cli import main


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "init")


def test_init_saves_sorted_deduped_roots_without_scanning(monkeypatch, tmp_path, capsys):
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    target = tmp_path / "config.toml"
    monkeypatch.setattr(config, "config_path", lambda: target)
    monkeypatch.setattr("version_drift.cli.config_path", lambda: target)
    monkeypatch.setattr("version_drift.cli.scan_projects", lambda *a, **k: pytest.fail("init must not scan"))

    rc = main(["init", str(two), str(one), str(two), "--json"])

    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert result["roots"] == [str(one.resolve()), str(two.resolve())]
    first = target.read_bytes()
    assert main(["init", str(one), str(two)]) == 0
    capsys.readouterr()
    assert target.read_bytes() == first


def test_init_defaults_to_cwd_and_invalid_root_keeps_old_config(monkeypatch, tmp_path, capsys):
    target = tmp_path / "config.toml"
    target.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(config, "config_path", lambda: target)
    monkeypatch.setattr("version_drift.cli.config_path", lambda: target)
    monkeypatch.chdir(tmp_path)

    assert main(["init", str(tmp_path / "missing")]) == 2
    assert target.read_text(encoding="utf-8") == "old\n"
    capsys.readouterr()
    assert main(["init", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["roots"] == [str(tmp_path.resolve())]


def test_root_resolution_precedence(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit"
    saved = tmp_path / "saved"
    env = tmp_path / "env"
    for path in (explicit, saved, env):
        path.mkdir()
    monkeypatch.setattr(config, "load_config", lambda: [str(saved)])
    monkeypatch.setenv("VERSION_DRIFT_ROOTS", str(env))
    assert config.resolve_roots([str(explicit)]) == [str(explicit.resolve())]
    assert config.resolve_roots([]) == [str(saved)]
    monkeypatch.setattr(config, "load_config", lambda: [])
    assert config.resolve_roots([]) == [str(env.resolve())]
    monkeypatch.delenv("VERSION_DRIFT_ROOTS")
    monkeypatch.chdir(tmp_path)
    assert config.resolve_roots([]) == [str(tmp_path.resolve())]


@pytest.mark.parametrize(
    "content",
    [
        'schema_version = 999\nroots = [\n  "/tmp",\n]\n',
        'schema_version = 1\nroots = [\n  "/tmp",\n',
        'schema_version = 1\nroots = [\n  42,\n]\n',
    ],
)
def test_saved_config_rejects_unsupported_or_malformed_content(tmp_path, content):
    target = tmp_path / "config.toml"
    target.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="invalid VersionDrift config"):
        config.load_config(target)


def test_saved_config_revalidates_stale_roots(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text(
        'schema_version = 1\nroots = [\n  "/definitely/missing/version-drift-root",\n]\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="existing directory"):
        config.load_config(target)


def test_env_roots_use_os_path_separator(monkeypatch, tmp_path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    monkeypatch.setattr(config, "load_config", lambda: [])
    monkeypatch.setenv("VERSION_DRIFT_ROOTS", os.pathsep.join((str(one), str(two))))
    assert config.resolve_roots([]) == sorted((str(one.resolve()), str(two.resolve())))


def test_inbox_first_unchanged_changed_and_resolved(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    _init_repo(repo)
    reports = iter(
        [
            {"path": str(repo), "state": "protected", "ahead": 0, "behind": 0, "reasons": ["missing_upstream"], "head": "a", "upstream": None, "status_fingerprint": "clean"},
            {"path": str(repo), "state": "protected", "ahead": 0, "behind": 0, "reasons": ["missing_upstream"], "head": "a", "upstream": None, "status_fingerprint": "clean"},
            {"path": str(repo), "state": "ahead", "ahead": 1, "behind": 0, "reasons": ["local_ahead_of_upstream"], "head": "b", "upstream": "origin/main", "status_fingerprint": "clean"},
            {"path": str(repo), "state": "synced", "ahead": 0, "behind": 0, "reasons": [], "head": "b", "upstream": "origin/main", "status_fingerprint": "clean"},
        ]
    )

    def fake_scan(*args, **kwargs):
        return {"projects": [next(reports)], "summary": {"working_files_changed": 0}}

    monkeypatch.setattr(inbox, "scan_projects", fake_scan)
    first = inbox.build_inbox([str(tmp_path)], base_dir=str(state))
    second = inbox.build_inbox([str(tmp_path)], base_dir=str(state))
    changed = inbox.build_inbox([str(tmp_path)], base_dir=str(state))
    resolved = inbox.build_inbox([str(tmp_path)], base_dir=str(state))

    assert first["first_run"] is True and first["counts"] == {"new": 1, "changed": 0, "resolved": 0}
    assert second["counts"] == {"new": 0, "changed": 0, "resolved": 0}
    assert changed["counts"] == {"new": 0, "changed": 1, "resolved": 0}
    assert changed["changed"][0]["previous"]["state"] == "protected"
    assert resolved["counts"] == {"new": 0, "changed": 0, "resolved": 1}


def test_inbox_corrupt_snapshot_fails_closed(monkeypatch, tmp_path, capsys):
    root = tmp_path / "root"
    root.mkdir()
    state = tmp_path / "state"
    path = inbox.snapshot_path(str(state))
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr("version_drift.cli.resolve_roots", lambda roots: [str(root)])

    rc = main(["--base-dir", str(state), "inbox", "--json"])

    assert rc == 1
    assert path.read_bytes() == before
    assert "invalid inbox snapshot" in capsys.readouterr().err


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"schema": inbox.SNAPSHOT_SCHEMA, "created_at": "now", "roots": [], "repos": {"/repo": "bad"}},
    ],
)
def test_inbox_wrong_json_shapes_fail_closed(monkeypatch, tmp_path, capsys, payload):
    root = tmp_path / "root"
    root.mkdir()
    state = tmp_path / "state"
    path = inbox.snapshot_path(str(state))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr("version_drift.cli.resolve_roots", lambda roots: [str(root)])

    rc = main(["--base-dir", str(state), "inbox", "--json"])

    assert rc == 1
    assert path.read_bytes() == before
    assert "invalid inbox snapshot" in capsys.readouterr().err


def test_inbox_unobserved_repository_is_not_falsely_resolved(monkeypatch, tmp_path):
    repo = str((tmp_path / "repo").resolve())
    path = tmp_path / "snapshot.json"
    prior_record = {
        "state": "ahead",
        "ahead": 1,
        "behind": 0,
        "reasons": ["local_ahead_of_upstream"],
        "head": "a",
        "upstream": "origin/main",
        "status_fingerprint": "clean",
    }
    path.write_text(
        json.dumps(
            {
                "schema": inbox.SNAPSHOT_SCHEMA,
                "created_at": "old",
                "roots": [str(tmp_path)],
                "repos": {repo: prior_record},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(inbox, "snapshot_path", lambda base_dir=None: path)
    monkeypatch.setattr(
        inbox,
        "scan_projects",
        lambda *a, **k: {"projects": [], "summary": {"working_files_changed": 0}},
    )

    result = inbox.build_inbox([str(tmp_path)])

    assert result["counts"] == {"new": 0, "changed": 0, "resolved": 0}
    assert json.loads(path.read_text(encoding="utf-8"))["repos"] == {repo: prior_record}


def test_inbox_resolves_only_when_repository_is_observed_synced(monkeypatch, tmp_path):
    repo = str((tmp_path / "repo").resolve())
    path = tmp_path / "snapshot.json"
    prior_record = {
        "state": "ahead",
        "ahead": 1,
        "behind": 0,
        "reasons": ["local_ahead_of_upstream"],
        "head": "a",
        "upstream": "origin/main",
        "status_fingerprint": "clean",
    }
    path.write_text(
        json.dumps(
            {
                "schema": inbox.SNAPSHOT_SCHEMA,
                "created_at": "old",
                "roots": [str(tmp_path)],
                "repos": {repo: prior_record},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(inbox, "snapshot_path", lambda base_dir=None: path)
    monkeypatch.setattr(
        inbox,
        "scan_projects",
        lambda *a, **k: {
            "projects": [
                {
                    "path": repo,
                    "state": "synced",
                    "ahead": 0,
                    "behind": 0,
                    "reasons": [],
                    "head": "b",
                    "upstream": "origin/main",
                    "status_fingerprint": "clean",
                }
            ],
            "summary": {"working_files_changed": 0},
        },
    )

    result = inbox.build_inbox([str(tmp_path)])

    assert result["counts"] == {"new": 0, "changed": 0, "resolved": 1}
    assert json.loads(path.read_text(encoding="utf-8"))["repos"] == {}


def test_inbox_snapshot_write_failure_keeps_previous(monkeypatch, tmp_path):
    path = tmp_path / "snapshot.json"
    old = {"schema": inbox.SNAPSHOT_SCHEMA, "created_at": "old", "roots": [], "repos": {}}
    path.write_text(json.dumps(old), encoding="utf-8")
    monkeypatch.setattr(inbox, "snapshot_path", lambda base_dir=None: path)
    monkeypatch.setattr(
        inbox,
        "scan_projects",
        lambda *a, **k: {"projects": [], "summary": {"working_files_changed": 0}},
    )
    monkeypatch.setattr(inbox, "atomic_write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        inbox.build_inbox([str(tmp_path)])

    assert json.loads(path.read_text(encoding="utf-8")) == old


def test_real_daily_loop_init_scan_inbox_change_resolve(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    root = tmp_path / "projects"
    repo = root / "repo"
    home.mkdir()
    root.mkdir()
    _init_repo(repo)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("VERSION_DRIFT_DIR", raising=False)
    monkeypatch.delenv("VERSION_DRIFT_ROOTS", raising=False)

    assert main(["init", str(root), "--json"]) == 0
    init_result = json.loads(capsys.readouterr().out)
    assert init_result["roots"] == [str(root.resolve())]

    assert main(["scan", "--json"]) == 0
    scan_result = json.loads(capsys.readouterr().out)
    assert scan_result["summary"]["total"] == 1
    assert scan_result["summary"]["working_files_changed"] == 0

    assert main(["inbox", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["counts"] == {"new": 1, "changed": 0, "resolved": 0}

    assert main(["inbox", "--json"]) == 0
    unchanged = json.loads(capsys.readouterr().out)
    assert unchanged["counts"] == {"new": 0, "changed": 0, "resolved": 0}

    (repo / "notes.txt").write_text("local work\n", encoding="utf-8")
    assert main(["inbox", "--json"]) == 0
    changed = json.loads(capsys.readouterr().out)
    assert changed["counts"] == {"new": 0, "changed": 1, "resolved": 0}
    assert changed["changed"][0]["current"]["state"] == "protected"

    (repo / "notes.txt").unlink()
    assert main(["inbox", "--json"]) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["counts"] == {"new": 0, "changed": 1, "resolved": 0}
    assert resolved["changed"][0]["current"]["status_fingerprint"] != changed["changed"][0]["current"]["status_fingerprint"]
    assert _git(repo, "status", "--porcelain=v1", "-uall") == ""


def test_cli_inbox_json_uses_explicit_root(monkeypatch, tmp_path, capsys):
    root = tmp_path / "root"
    root.mkdir()
    seen = []

    def fake_build(roots, **kwargs):
        seen.extend(roots)
        return {
            "schema": inbox.SNAPSHOT_SCHEMA,
            "generated_at": "now",
            "previous_snapshot_at": None,
            "first_run": True,
            "new": [],
            "changed": [],
            "resolved": [],
            "counts": {"new": 0, "changed": 0, "resolved": 0},
            "scan_summary": {"working_files_changed": 0},
        }

    monkeypatch.setattr("version_drift.cli.build_inbox", fake_build)
    rc = main(["inbox", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert seen == [str(root.resolve())]
    assert payload["schema"] == inbox.SNAPSHOT_SCHEMA
