import json
import os
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from version_drift.integrate import (
    INTENT_SCHEMA,
    IntegrationIntent,
    IntegrationIntentStore,
)


OID_A = "a" * 40
OID_B = "b" * 40
OID_C = "c" * 40


def intent(**changes):
    value = IntegrationIntent(
        schema=INTENT_SCHEMA,
        intent_id="intent-001",
        agent_id="agent-7",
        repository_path="/work/example",
        repository_id="github.com/example/project",
        source_ref="refs/heads/feature",
        target_ref="refs/heads/main",
        base_oid=OID_A,
        source_oid=OID_B,
        target_oid=OID_C,
        summary="Integrate the completed feature",
        dependency_intent_ids=("intent-000",),
        created_at="2026-08-22T10:20:30Z",
    )
    return replace(value, **changes) if changes else value


def test_intent_round_trips_as_versioned_immutable_json_envelope():
    original = intent()
    payload = original.to_dict()

    assert payload == {
        "schema": "version-drift/integration-intent/1",
        "intent_id": "intent-001",
        "agent_id": "agent-7",
        "repository_path": "/work/example",
        "repository_id": "github.com/example/project",
        "source_ref": "refs/heads/feature",
        "target_ref": "refs/heads/main",
        "base_oid": OID_A,
        "source_oid": OID_B,
        "target_oid": OID_C,
        "summary": "Integrate the completed feature",
        "dependency_intent_ids": ["intent-000"],
        "created_at": "2026-08-22T10:20:30Z",
    }
    assert IntegrationIntent.from_dict(payload) == original
    with pytest.raises(FrozenInstanceError):
        original.summary = "changed"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("summary"),
        lambda p: p.update(extra=True),
        lambda p: p.update(schema="version-drift/integration-intent/2"),
        lambda p: p.update(intent_id="../escape"),
        lambda p: p.update(intent_id="nested/name"),
        lambda p: p.update(agent_id=""),
        lambda p: p.update(repository_path="relative/repo"),
        lambda p: p.update(repository_path="/work/../secret"),
        lambda p: p.update(repository_id=7),
        lambda p: p.update(source_ref=""),
        lambda p: p.update(target_ref=3),
        lambda p: p.update(base_oid="a" * 39),
        lambda p: p.update(source_oid="A" * 40),
        lambda p: p.update(target_oid=4),
        lambda p: p.update(summary=""),
        lambda p: p.update(dependency_intent_ids="intent-000"),
        lambda p: p.update(dependency_intent_ids=["intent-000", "intent-000"]),
        lambda p: p.update(dependency_intent_ids=["intent-001"]),
        lambda p: p.update(dependency_intent_ids=["../bad"]),
        lambda p: p.update(created_at="2026-08-22T10:20:30"),
        lambda p: p.update(created_at="2026-08-22T10:20:30+01:00"),
        lambda p: p.update(created_at="not-a-time"),
    ],
)
def test_validation_fails_closed_for_malformed_envelopes(mutate):
    payload = intent().to_dict()
    mutate(payload)

    with pytest.raises(ValueError, match="invalid integration intent"):
        IntegrationIntent.from_dict(payload)


def test_wrong_top_level_and_bool_string_confusion_are_rejected():
    with pytest.raises(ValueError, match="invalid integration intent"):
        IntegrationIntent.from_dict([])
    payload = intent().to_dict()
    payload["summary"] = True
    with pytest.raises(ValueError, match="invalid integration intent"):
        IntegrationIntent.from_dict(payload)


def test_store_creates_outside_repository_and_loads_exact_value(tmp_path):
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir()
    original = intent(repository_path=str(repo.resolve()))
    store = IntegrationIntentStore(base_dir=state)

    path = store.create(original)

    assert path == state / ".version-drift" / "integration-intents" / "intent-001.json"
    assert not (repo / ".version-drift").exists()
    assert store.load("intent-001") == original
    assert json.loads(path.read_text(encoding="utf-8")) == original.to_dict()
    assert not list(path.parent.glob(".*.tmp"))


def test_create_is_immutable_and_never_overwrites_existing_id(tmp_path):
    store = IntegrationIntentStore(base_dir=tmp_path)
    path = store.create(intent())
    before = path.read_bytes()

    with pytest.raises(FileExistsError):
        store.create(intent(summary="replacement"))

    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["directory-symlink", "dangling-symlink", "file"])
def test_create_rejects_unsafe_final_store_path_without_writing(kind, tmp_path):
    store = IntegrationIntentStore(base_dir=tmp_path / "state")
    store.directory.parent.mkdir(parents=True)
    target = tmp_path / "intent-target"
    if kind == "file":
        store.directory.write_text("not a directory", encoding="utf-8")
    else:
        if kind == "directory-symlink":
            target.mkdir()
        try:
            store.directory.symlink_to(target, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises((OSError, ValueError), match="symlink|integration-intents|intent store"):
        store.create(intent())

    if kind == "directory-symlink":
        assert list(target.iterdir()) == []
    elif kind == "dangling-symlink":
        assert not target.exists()
    else:
        assert store.directory.read_text(encoding="utf-8") == "not a directory"


def test_load_and_create_reject_path_traversal_ids(tmp_path):
    store = IntegrationIntentStore(base_dir=tmp_path)
    for bad_id in ("../escape", "sub/file", ".", "..", "\\windows"):
        with pytest.raises(ValueError, match="invalid integration intent"):
            store.load(bad_id)
    with pytest.raises(ValueError, match="invalid integration intent"):
        store.create(intent(intent_id="../escape"))
    assert not (tmp_path / ".version-drift" / "escape.json").exists()


def test_list_is_sorted_and_validates_every_stored_envelope(tmp_path):
    store = IntegrationIntentStore(base_dir=tmp_path)
    store.create(intent(intent_id="z-last", dependency_intent_ids=()))
    store.create(intent(intent_id="a-first", dependency_intent_ids=()))

    assert [item.intent_id for item in store.list()] == ["a-first", "z-last"]

    corrupt = store.directory / "middle.json"
    corrupt.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid integration intent"):
        store.list()


def test_list_treats_genuinely_absent_store_as_empty(tmp_path):
    store = IntegrationIntentStore(base_dir=tmp_path / "missing" / "state")

    assert store.list() == []
    assert not store.directory.exists()


def test_list_rejects_regular_file_store_path(tmp_path):
    store = IntegrationIntentStore(base_dir=tmp_path)
    store.directory.parent.mkdir(parents=True)
    store.directory.write_text("not a directory", encoding="utf-8")

    with pytest.raises((OSError, ValueError), match="integration-intents|intent store"):
        store.list()


@pytest.mark.parametrize("dangling", [False, True])
def test_list_rejects_symlinked_store_path(tmp_path, dangling):
    store = IntegrationIntentStore(base_dir=tmp_path / "state")
    store.directory.parent.mkdir(parents=True)
    target = tmp_path / "missing-target"
    if not dangling:
        target.mkdir()
    try:
        store.directory.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises((OSError, ValueError), match="symlink|integration-intents|intent store"):
        store.list()


def test_load_rejects_filename_envelope_id_mismatch(tmp_path):
    store = IntegrationIntentStore(base_dir=tmp_path)
    store.directory.mkdir(parents=True)
    path = store.directory / "other.json"
    path.write_text(json.dumps(intent().to_dict()), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match"):
        store.load("other")


def test_load_rejects_duplicate_json_fields(tmp_path):
    store = IntegrationIntentStore(base_dir=tmp_path)
    store.directory.mkdir(parents=True)
    text = json.dumps(intent().to_dict())
    text = text.replace('"summary":', '"summary": "shadowed", "summary":', 1)
    (store.directory / "intent-001.json").write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate field"):
        store.load("intent-001")


def test_default_store_obeys_version_drift_state_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("VERSION_DRIFT_DIR", str(tmp_path / "state"))

    store = IntegrationIntentStore()

    assert store.directory == tmp_path / "state" / ".version-drift" / "integration-intents"


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative race regression is POSIX-only")
def test_create_stays_anchored_when_store_is_replaced_after_open(monkeypatch, tmp_path):
    import version_drift.integrate.intent as module

    store = IntegrationIntentStore(base_dir=tmp_path / "state")
    store.directory.mkdir(parents=True)
    original = tmp_path / "opened-store"
    attacker = tmp_path / "attacker"
    attacker.mkdir()

    def replace_store(_directory_fd):
        store.directory.rename(original)
        store.directory.symlink_to(attacker, target_is_directory=True)

    monkeypatch.setattr(module, "_store_opened", replace_store)
    result = store.create(intent())

    assert result == store.directory / "intent-001.json"
    assert list(attacker.iterdir()) == []
    assert json.loads((original / "intent-001.json").read_text(encoding="utf-8")) == intent().to_dict()
    assert not list(original.glob(".*.tmp"))


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative race regression is POSIX-only")
@pytest.mark.parametrize("operation", ["list", "load"])
def test_reads_stay_anchored_when_store_is_replaced_after_open(
    monkeypatch, tmp_path, operation
):
    import version_drift.integrate.intent as module

    store = IntegrationIntentStore(base_dir=tmp_path / "state")
    store.create(intent())
    original = tmp_path / "opened-store"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    external = intent(summary="attacker-controlled")
    (attacker / "intent-001.json").write_text(
        json.dumps(external.to_dict()), encoding="utf-8"
    )

    def replace_store(_directory_fd):
        store.directory.rename(original)
        store.directory.symlink_to(attacker, target_is_directory=True)

    monkeypatch.setattr(module, "_store_opened", replace_store)
    result = store.list() if operation == "list" else [store.load("intent-001")]

    assert result == [intent()]


@pytest.mark.skipif(os.name != "posix", reason="symlink regression is POSIX-only")
@pytest.mark.parametrize("operation", ["list", "load"])
def test_reads_reject_symlinked_json_entry(operation, tmp_path):
    store = IntegrationIntentStore(base_dir=tmp_path / "state")
    store.directory.mkdir(parents=True)
    external = tmp_path / "external.json"
    external.write_text(json.dumps(intent().to_dict()), encoding="utf-8")
    (store.directory / "intent-001.json").symlink_to(external)

    with pytest.raises(ValueError, match="regular file|symlink|cannot load"):
        if operation == "list":
            store.list()
        else:
            store.load("intent-001")
