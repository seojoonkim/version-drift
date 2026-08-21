import hashlib
import json
from pathlib import Path

import pytest

import version_drift.core as core


def _report(path: Path, *, state: str = "behind_clean", ok: bool = False) -> dict:
    eligible = state == "behind_clean"
    return {
        "schema": "version-drift/1",
        "checked_at": "2026-08-22T00:00:00+00:00",
        "path": str(path.resolve()),
        "state": state,
        "safe_to_update": eligible,
        "eligibility": "eligible" if eligible else "blocked",
        "relation": "behind" if eligible else "unknown",
        "head": "abc",
        "upstream": "origin/main",
        "status_fingerprint": "clean",
        "ok": ok,
        "reasons": ["local_behind_upstream"] if eligible else [],
        "reason_codes": ["local_behind_upstream"] if eligible else [],
        "actions": [],
    }


def _events(state: Path) -> list:
    event_file = state / ".version-drift" / "events.jsonl"
    return [json.loads(line) for line in event_file.read_text(encoding="utf-8").splitlines()]


def _install_inspections(monkeypatch, reports):
    reports = iter(reports)
    monkeypatch.setattr(core, "inspect_project", lambda path, fetch=False: next(reports))


def test_verified_apply_records_lifecycle_before_legacy_success(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    before = _report(repo)
    current = _report(repo)
    after = _report(repo, state="synced", ok=True)
    _install_inspections(monkeypatch, [before, current, after])
    monkeypatch.setattr(core, "_run_git", lambda *args, **kwargs: core.GitResult(True, "updated"))

    result = core.sync_project(str(repo), base_dir=str(state), apply=True, fetch=False)

    assert result["applied"] is True
    assert result["reason"] == "fast_forward_applied"
    rows = _events(state)
    assert [row["event"] for row in rows] == [
        "apply_started", "apply_verified_success", "sync_applied"
    ]
    assert rows[-1]["schema"] == "version-drift/1"


def test_pull_failure_records_lifecycle_before_legacy_failure(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    _install_inspections(monkeypatch, [_report(repo), _report(repo), _report(repo)])
    monkeypatch.setattr(
        core,
        "_run_git",
        lambda *args, **kwargs: core.GitResult(False, stderr="network down", returncode=1),
    )

    result = core.sync_project(str(repo), base_dir=str(state), apply=True, fetch=False)

    assert result["applied"] is False
    assert result["reason"] == "fast_forward_failed"
    assert [row["event"] for row in _events(state)] == [
        "apply_started", "apply_failed", "sync_failed"
    ]


def test_unverified_success_is_unknown_and_operationally_failed(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    reports = [_report(repo), _report(repo), _report(repo, state="protected", ok=False)]
    _install_inspections(monkeypatch, reports)
    monkeypatch.setattr(core, "discover_projects", lambda roots, max_depth=5: [repo])
    monkeypatch.setattr(core, "_repository_snapshots", lambda paths: {})
    monkeypatch.setattr(core, "_run_git", lambda *args, **kwargs: core.GitResult(True, "updated"))

    result = core.sync_projects(
        [str(repo)], base_dir=str(state), apply=True, fetch=False, max_depth=0
    )

    project = result["projects"][0]
    assert project["applied"] is False
    assert project["reason"] == "fast_forward_outcome_unknown"
    assert result["outcome"] == "failed"
    assert result["summary"]["failed"] == 1
    assert [row["event"] for row in _events(state)] == [
        "apply_started", "apply_outcome_unknown", "sync_failed"
    ]


def test_precreated_atomic_lock_refuses_apply_without_pull_and_is_preserved(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    canonical = str(repo.resolve())
    lock = state / ".version-drift" / "locks" / (
        hashlib.sha256(canonical.encode("utf-8")).hexdigest() + ".lock"
    )
    lock.parent.mkdir(parents=True)
    lock.write_text("other owner\n", encoding="utf-8")
    _install_inspections(monkeypatch, [_report(repo)])
    pulls = []
    monkeypatch.setattr(core, "_run_git", lambda *args, **kwargs: pulls.append(args))

    result = core.sync_project(str(repo), base_dir=str(state), apply=True, fetch=False)

    assert result["blocked"] is True
    assert result["applied"] is False
    assert result["reason"] == "apply_lock_held"
    assert pulls == []
    assert lock.read_text(encoding="utf-8") == "other owner\n"
    assert [row["event"] for row in _events(state)] == ["sync_blocked"]


@pytest.mark.parametrize("pull_ok", [True, False])
def test_owned_lock_is_released_after_success_and_failure(monkeypatch, tmp_path, pull_ok):
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    after = _report(repo, state="synced", ok=True) if pull_ok else _report(repo)
    _install_inspections(monkeypatch, [_report(repo), _report(repo), after])
    monkeypatch.setattr(
        core,
        "_run_git",
        lambda *args, **kwargs: core.GitResult(pull_ok, returncode=0 if pull_ok else 1),
    )

    core.sync_project(str(repo), base_dir=str(state), apply=True, fetch=False)

    locks = state / ".version-drift" / "locks"
    assert locks.is_dir()
    assert list(locks.iterdir()) == []


def test_lock_paths_are_repo_specific_and_canonical(tmp_path):
    state = tmp_path / "state"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    first_lock = core._apply_lock_path(str(first), str(state))
    alias_lock = core._apply_lock_path(str(first / ".." / "first"), str(state))
    second_lock = core._apply_lock_path(str(second), str(state))

    assert first_lock == alias_lock
    assert first_lock != second_lock
    assert first_lock.parent == state / ".version-drift" / "locks"
    assert first_lock.name == hashlib.sha256(
        str(first.resolve()).encode("utf-8")
    ).hexdigest() + ".lock"

    first_lock.parent.mkdir(parents=True)
    descriptor = core._acquire_apply_lock(str(first), str(state))
    try:
        assert descriptor is not None
        assert core._acquire_apply_lock(str(first), str(state)) is None
        other_descriptor = core._acquire_apply_lock(str(second), str(state))
        assert other_descriptor is not None
        core._release_apply_lock(second_lock, other_descriptor)
    finally:
        core._release_apply_lock(first_lock, descriptor)
    assert not first_lock.exists()
    assert not second_lock.exists()
