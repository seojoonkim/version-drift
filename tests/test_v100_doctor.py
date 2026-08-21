import json
import subprocess
from pathlib import Path

import version_drift.config as config
import version_drift.doctor as doctor
from version_drift.cli import main


CHECK_NAMES = [
    "python_runtime",
    "git_executable",
    "config",
    "event_history",
    "inbox_snapshot",
    "apply_locks",
    "state_directory",
]


def _run(monkeypatch, tmp_path, capsys, *extra):
    base = tmp_path / "base"
    config_file = tmp_path / "config" / "config.toml"
    monkeypatch.setattr(config, "config_path", lambda: config_file)
    seen = []

    def git_version(command, **kwargs):
        seen.append(command)
        assert command == ["git", "--version"]
        return subprocess.CompletedProcess(command, 0, "git version 2.45.0\n", "")

    monkeypatch.setattr(doctor.subprocess, "run", git_version)
    rc = main(["--base-dir", str(base), "doctor", *extra])
    return rc, capsys.readouterr(), base, config_file, seen


def test_doctor_healthy_json_has_exact_schema_and_stable_order(monkeypatch, tmp_path, capsys):
    rc, captured, base, config_file, seen = _run(monkeypatch, tmp_path, capsys, "--json")

    payload = json.loads(captured.out)
    assert rc == 0
    assert captured.err == ""
    assert set(payload) == {"schema", "ok", "checks"}
    assert payload["schema"] == "version-drift/doctor/1"
    assert payload["ok"] is True
    assert [item["name"] for item in payload["checks"]] == CHECK_NAMES
    assert all(set(item) >= {"name", "ok", "detail"} for item in payload["checks"])
    assert all(item["ok"] for item in payload["checks"])
    assert seen == [["git", "--version"]]
    assert not base.exists()
    assert not config_file.parent.exists()


def test_doctor_corrupt_config_is_issue_and_preserved(monkeypatch, tmp_path, capsys):
    config_file = tmp_path / "config" / "config.toml"
    config_file.parent.mkdir()
    config_file.write_bytes(b"not valid\n")
    before = config_file.read_bytes()

    rc, captured, _, _, _ = _run(monkeypatch, tmp_path, capsys, "--json")
    payload = json.loads(captured.out)
    check = next(item for item in payload["checks"] if item["name"] == "config")
    assert rc == 1 and payload["ok"] is False and check["ok"] is False
    assert "invalid" in check["detail"].lower()
    assert config_file.read_bytes() == before


def test_doctor_counts_malformed_events_without_repair(monkeypatch, tmp_path, capsys):
    event = tmp_path / "base" / ".version-drift" / "events.jsonl"
    event.parent.mkdir(parents=True)
    event.write_bytes(b'{"event":"scan"}\nnot json\n[]\n\xff\n')
    before = event.read_bytes()

    rc, captured, _, _, _ = _run(monkeypatch, tmp_path, capsys, "--json")
    payload = json.loads(captured.out)
    check = next(item for item in payload["checks"] if item["name"] == "event_history")
    assert rc == 1 and check["ok"] is False
    assert check["counts"] == {"malformed_lines": 3, "source_lines": 4}
    assert event.read_bytes() == before


def test_doctor_corrupt_snapshot_is_issue_and_preserved(monkeypatch, tmp_path, capsys):
    snapshot = tmp_path / "base" / ".version-drift" / "inbox_snapshot.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b"not json")
    before = snapshot.read_bytes()

    rc, captured, _, _, _ = _run(monkeypatch, tmp_path, capsys, "--json")
    check = next(item for item in json.loads(captured.out)["checks"] if item["name"] == "inbox_snapshot")
    assert rc == 1 and check["ok"] is False
    assert "invalid inbox snapshot" in check["detail"]
    assert snapshot.read_bytes() == before


def test_doctor_reports_locks_and_preserves_them(monkeypatch, tmp_path, capsys):
    lock = tmp_path / "base" / ".version-drift" / "locks" / "held.lock"
    lock.parent.mkdir(parents=True)
    lock.write_bytes(b"do not parse or delete")
    before = lock.read_bytes()

    rc, captured, _, _, _ = _run(monkeypatch, tmp_path, capsys, "--json")
    check = next(item for item in json.loads(captured.out)["checks"] if item["name"] == "apply_locks")
    assert rc == 1 and check["ok"] is False
    assert check["counts"] == {"locks": 1}
    assert check["data"] == ["held.lock"]
    assert "stale-or-active-unknown" in check["detail"]
    assert lock.read_bytes() == before


def test_doctor_is_byte_read_only_and_human_output_ends_safety_line(monkeypatch, tmp_path, capsys):
    state = tmp_path / "base" / ".version-drift"
    state.mkdir(parents=True)
    event = state / "events.jsonl"
    event.write_bytes(b'{"event":"scan"}\n')
    snapshot = state / "inbox_snapshot.json"
    snapshot.write_text(json.dumps({"schema": "version-drift/inbox/1", "created_at": "now", "roots": [], "repos": {}}), encoding="utf-8")
    config_file = tmp_path / "config" / "config.toml"
    config_file.parent.mkdir()
    config_file.write_text('schema_version = 1\nroots = [\n  "' + str(tmp_path) + '",\n]\n', encoding="utf-8")
    before = {path: path.read_bytes() for path in (event, snapshot, config_file)}

    rc, captured, _, _, _ = _run(monkeypatch, tmp_path, capsys)
    assert rc == 0
    assert captured.out.rstrip().endswith("Nothing was changed.")
    assert "VersionDrift doctor" in captured.out
    assert {path: path.read_bytes() for path in before} == before


def test_doctor_converts_individual_oserror_to_failed_check(monkeypatch, tmp_path):
    event = tmp_path / "base" / ".version-drift" / "events.jsonl"
    event.parent.mkdir(parents=True)
    event.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "_check_events", lambda path: (_ for _ in ()).throw(OSError("denied")))

    result = doctor.run_doctor(str(tmp_path / "base"))
    check = next(item for item in result["checks"] if item["name"] == "event_history")
    assert result["ok"] is False and check["ok"] is False
    assert "denied" in check["detail"]


def test_doctor_git_failure_is_issue_not_traceback(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "missing-config")
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("git missing")))
    result = doctor.run_doctor(str(tmp_path / "missing-state"))
    check = next(item for item in result["checks"] if item["name"] == "git_executable")
    assert result["ok"] is False and check["ok"] is False
    assert "git missing" in check["detail"]
    assert not (tmp_path / "missing-state").exists()


def test_doctor_usage_error_remains_argparse_two():
    try:
        main(["doctor", "--unknown"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("argparse should reject unknown arguments")


def test_doctor_python_minimum_check_can_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "missing-config")
    monkeypatch.setattr(doctor.sys, "version_info", (3, 8, 10))
    result = doctor.run_doctor(str(tmp_path / "missing-state"))
    check = result["checks"][0]
    assert check["name"] == "python_runtime" and check["ok"] is False
    assert result["ok"] is False


def test_state_path_existing_file_is_issue(monkeypatch, tmp_path):
    base = tmp_path / "base"
    state = base / ".version-drift"
    base.mkdir()
    state.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "missing-config")
    result = doctor.run_doctor(str(base))
    check = result["checks"][-1]
    assert check["name"] == "state_directory" and check["ok"] is False
    assert "not a directory" in check["detail"]
    assert state.read_text(encoding="utf-8") == "not a directory"


def test_git_check_uses_non_mutating_subprocess_options(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "missing-config")
    observed = {}

    def spy(command, **kwargs):
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "git version 2.45.0\n", "")

    monkeypatch.setattr(doctor.subprocess, "run", spy)
    doctor.run_doctor(str(tmp_path / "missing-state"))
    assert observed["check"] is False
    assert observed["text"] is True
    assert observed["stdout"] == subprocess.PIPE
    assert observed["stderr"] == subprocess.PIPE
    assert observed["timeout"] > 0
    assert "cwd" not in observed
