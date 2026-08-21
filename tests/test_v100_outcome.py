import json
from pathlib import Path

import version_drift.cli as cli
import version_drift.core as core


def _report(path: Path, *, eligible: bool = True, ok: bool = False) -> dict:
    return {
        "path": str(path),
        "state": "behind_clean" if eligible else "protected",
        "safe_to_update": eligible,
        "eligibility": "eligible" if eligible else "blocked",
        "head": "before",
        "upstream": "origin/main",
        "status_fingerprint": "clean",
        "ok": ok,
        "reasons": [] if eligible else ["dirty_worktree"],
    }


def _install_sync_fakes(monkeypatch, repos, pulls):
    inspections = {str(repo): 0 for repo in repos}

    def inspect(path, fetch=False):
        count = inspections[path]
        inspections[path] += 1
        if count < 2:
            return _report(Path(path))
        return {
            **_report(Path(path), eligible=False, ok=pulls[path].ok),
            "state": "synced" if pulls[path].ok else "behind_clean",
        }

    monkeypatch.setattr(core, "discover_projects", lambda roots, max_depth=5: repos)
    monkeypatch.setattr(core, "_repository_snapshots", lambda paths: {})
    monkeypatch.setattr(core, "inspect_project", inspect)
    monkeypatch.setattr(core, "_run_git", lambda repo, args, timeout_s=20.0: pulls[str(repo)])
    monkeypatch.setattr(core, "record_event", lambda *args, **kwargs: None)


def test_scan_envelope_has_schema_and_complete_outcome(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    monkeypatch.setattr(core, "discover_projects", lambda roots, max_depth=5: [repo])
    monkeypatch.setattr(core, "_repository_snapshots", lambda paths: {})
    monkeypatch.setattr(core, "inspect_project", lambda path, fetch=False: _report(repo, eligible=False))
    monkeypatch.setattr(core, "record_event", lambda *args, **kwargs: None)

    result = core.scan_projects([str(tmp_path)])

    assert result["schema"] == "version-drift/scan/1"
    assert result["outcome"] == "complete"
    assert "summary" in result and "projects" in result


def test_sync_dry_run_with_only_policy_protected_repository_is_complete(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    monkeypatch.setattr(core, "discover_projects", lambda roots, max_depth=5: [repo])
    monkeypatch.setattr(core, "_repository_snapshots", lambda paths: {})
    monkeypatch.setattr(core, "inspect_project", lambda path, fetch=False: _report(repo, eligible=False))
    monkeypatch.setattr(core, "record_event", lambda *args, **kwargs: None)

    result = core.sync_projects([str(repo)], apply=False, fetch=False, max_depth=0)

    assert result["schema"] == "version-drift/sync/1"
    assert result["outcome"] == "complete"
    assert result["summary"]["outcome"] == "complete"


def test_one_verified_apply_and_one_pull_failure_is_partial(monkeypatch, tmp_path, capsys):
    repos = [tmp_path / "a", tmp_path / "b"]
    for repo in repos:
        (repo / ".git").mkdir(parents=True)
    pulls = {
        str(repos[0]): core.GitResult(True, "updated"),
        str(repos[1]): core.GitResult(False, stderr="network failed", returncode=1),
    }
    _install_sync_fakes(monkeypatch, repos, pulls)

    result = core.sync_projects([str(tmp_path)], apply=True, fetch=False)

    assert result["outcome"] == result["summary"]["outcome"] == "partial"
    assert result["summary"]["applied"] == 1
    assert result["summary"]["failed"] == 1

    _install_sync_fakes(monkeypatch, repos, pulls)
    assert cli.main(["sync", str(tmp_path), "--apply", "--no-fetch", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["outcome"] == "partial"


def test_all_attempted_eligible_pulls_fail_is_failed(monkeypatch, tmp_path):
    repos = [tmp_path / "a", tmp_path / "b"]
    pulls = {str(repo): core.GitResult(False, stderr="no route", returncode=1) for repo in repos}
    _install_sync_fakes(monkeypatch, repos, pulls)

    result = core.sync_projects([str(tmp_path)], apply=True, fetch=False)

    assert result["outcome"] == result["summary"]["outcome"] == "failed"
    assert result["summary"]["applied"] == 0
    assert result["summary"]["failed"] == 2


def test_all_eligible_updates_verified_is_complete(monkeypatch, tmp_path):
    repos = [tmp_path / "a", tmp_path / "b"]
    pulls = {str(repo): core.GitResult(True, "updated") for repo in repos}
    _install_sync_fakes(monkeypatch, repos, pulls)

    result = core.sync_projects([str(tmp_path)], apply=True, fetch=False)

    assert result["outcome"] == result["summary"]["outcome"] == "complete"
    assert result["summary"]["applied"] == 2
    assert result["summary"]["failed"] == 0


def test_single_policy_block_apply_keeps_exit_one(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    payload = {
        "schema": "version-drift/sync/1",
        "outcome": "complete",
        "summary": {"outcome": "complete", "applied": 0, "safe": 0, "protected": 1,
                    "working_files_changed": 0, "failed": 0},
        "projects": [{"blocked": True, "applied": False}],
    }
    monkeypatch.setattr(cli, "sync_projects", lambda *args, **kwargs: payload)

    assert cli.main(["sync", str(repo), "--apply", "--no-fetch", "--json"]) == 1


def test_single_operational_failure_returns_exit_three(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    payload = {
        "schema": "version-drift/sync/1",
        "outcome": "failed",
        "summary": {"outcome": "failed", "applied": 0, "safe": 1, "protected": 0,
                    "working_files_changed": 0, "failed": 1},
        "projects": [{"blocked": True, "applied": False, "reason": "fast_forward_failed",
                      "pull": {"ok": False}}],
    }
    monkeypatch.setattr(cli, "sync_projects", lambda *args, **kwargs: payload)

    assert cli.main(["sync", str(repo), "--apply", "--no-fetch", "--json"]) == 3


def test_apply_time_state_change_is_blocked_but_not_operational_failure(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    inspections = iter([_report(repo), _report(repo, eligible=False)])
    monkeypatch.setattr(core, "discover_projects", lambda roots, max_depth=5: [repo])
    monkeypatch.setattr(core, "_repository_snapshots", lambda paths: {})
    monkeypatch.setattr(core, "inspect_project", lambda path, fetch=False: next(inspections))
    monkeypatch.setattr(core, "record_event", lambda *args, **kwargs: None)

    result = core.sync_projects([str(repo)], apply=True, fetch=False, max_depth=0)

    assert result["projects"][0]["reason"] == "state_changed_before_apply"
    assert result["outcome"] == "complete"
    assert result["summary"]["failed"] == 0
