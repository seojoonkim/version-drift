from pathlib import Path
import subprocess

import version_drift.core as core


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def _repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "a").write_text("a\n", encoding="utf-8")
    _git(path, "add", "a")
    _git(path, "commit", "-m", "initial")


def test_every_early_return_has_orthogonal_facts(tmp_path):
    missing = core.inspect_project(str(tmp_path / "missing"))
    _repo(tmp_path / "repo")
    no_upstream = core.inspect_project(str(tmp_path / "repo"))

    for report in (missing, no_upstream):
        assert report["relation"] in {"in_sync", "behind", "ahead", "diverged", "unknown"}
        assert report["eligibility"] in {"eligible", "blocked", "unknown"}
        assert report["reason_codes"] == report["reasons"]
    assert missing["relation"] == "unknown"
    assert missing["eligibility"] == "unknown"
    assert no_upstream["eligibility"] == "unknown"


def test_synced_report_has_explicit_no_action_reason(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)

    def fake(path, args, timeout_s=20.0, **kwargs):
        command = list(args)
        if command == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]:
            return core.GitResult(True, "origin/main")
        if command == ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"]:
            return core.GitResult(True, "0 0")
        return real(path, args, timeout_s=timeout_s, **kwargs)

    real = core._run_git
    monkeypatch.setattr(core, "_run_git", fake)
    report = core.inspect_project(str(repo))
    assert report["state"] == "synced"
    assert report["relation"] == "in_sync"
    assert report["eligibility"] == "blocked"
    assert report["reason_codes"] == report["reasons"] == ["in_sync_no_action"]


def test_behind_clean_is_the_only_eligible_class(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)
    real = core._run_git

    def fake(path, args, timeout_s=20.0, **kwargs):
        command = list(args)
        if command == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]:
            return core.GitResult(True, "origin/main")
        if command == ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"]:
            return core.GitResult(True, "0 2")
        return real(path, args, timeout_s=timeout_s, **kwargs)

    monkeypatch.setattr(core, "_run_git", fake)
    report = core.inspect_project(str(repo))
    assert report["state"] == "behind_clean"
    assert report["relation"] == "behind"
    assert report["eligibility"] == "eligible"
    assert report["reason_codes"] == report["reasons"]
