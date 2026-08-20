import json
import subprocess
from pathlib import Path

from version_drift import inspect_project, record_event
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


def test_inspect_and_event_store(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    report = inspect_project(str(repo))
    assert report["schema"] == "version-drift/1"
    assert "missing_upstream" in report["reasons"]
    event = record_event(report, base_dir=str(tmp_path / "state"), event="inspect")
    assert event["event"] == "inspect"
    assert (tmp_path / "state" / ".version-drift" / "events.jsonl").exists()


def test_cli_help_and_json(tmp_path, capsys):
    repo = tmp_path / "repo"
    _init_repo(repo)
    rc = main(["--base-dir", str(tmp_path / "state"), "inspect", str(repo), "--json"])
    assert rc == 1
    out = capsys.readouterr().out
    assert '"schema": "version-drift/1"' in out
    assert '"state": "blocked"' in out
