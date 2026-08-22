"""Versioned, immutable integration intent envelopes and local storage."""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from version_drift.atomicio import atomic_write_text
from version_drift.core import _event_path

INTENT_SCHEMA = "version-drift/integration-intent/1"
_FIELDS = {
    "schema",
    "intent_id",
    "agent_id",
    "repository_path",
    "repository_id",
    "source_ref",
    "target_ref",
    "base_oid",
    "source_oid",
    "target_oid",
    "summary",
    "dependency_intent_ids",
    "created_at",
}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_OID = re.compile(r"[0-9a-f]{40}\Z")
_UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z\Z"
)


def _store_opened(directory_fd: int) -> None:
    """Test hook called after the store directory has been securely opened."""


def _fail(message: str) -> ValueError:
    return ValueError("invalid integration intent: " + message)


def _unique_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _fail(f"duplicate field: {key}")
        result[key] = value
    return result


def _nonempty_string(value: Any, field: str) -> str:
    if type(value) is not str or not value or any(ord(char) < 32 for char in value):
        raise _fail(f"{field} must be a nonempty string without control characters")
    return value


def _intent_id(value: Any, field: str = "intent_id") -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise _fail(f"{field} is malformed")
    return value


@dataclass(frozen=True)
class IntegrationIntent:
    """A validated immutable description of an integration requested by an agent."""

    schema: str
    intent_id: str
    agent_id: str
    repository_path: str
    repository_id: str
    source_ref: str
    target_ref: str
    base_oid: str
    source_oid: str
    target_oid: str
    summary: str
    dependency_intent_ids: Tuple[str, ...]
    created_at: str

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if self.schema != INTENT_SCHEMA:
            raise _fail("unsupported schema")
        _intent_id(self.intent_id)
        _nonempty_string(self.agent_id, "agent_id")
        repository_path = _nonempty_string(self.repository_path, "repository_path")
        path = Path(repository_path)
        if not path.is_absolute() or str(path) != str(Path(os.path.normpath(repository_path))):
            raise _fail("repository_path must be an absolute normalized path")
        for field in ("repository_id", "source_ref", "target_ref", "summary"):
            _nonempty_string(getattr(self, field), field)
        for field in ("base_oid", "source_oid", "target_oid"):
            value = getattr(self, field)
            if type(value) is not str or _OID.fullmatch(value) is None:
                raise _fail(f"{field} must be a full lowercase 40-hex object ID")
        if type(self.dependency_intent_ids) is not tuple:
            raise _fail("dependency_intent_ids must be a list")
        for dependency in self.dependency_intent_ids:
            _intent_id(dependency, "dependency intent ID")
        if len(set(self.dependency_intent_ids)) != len(self.dependency_intent_ids):
            raise _fail("dependency intent IDs must be unique")
        if self.intent_id in self.dependency_intent_ids:
            raise _fail("an intent cannot depend on itself")
        if type(self.created_at) is not str or _UTC_TIMESTAMP.fullmatch(self.created_at) is None:
            raise _fail("created_at must be a UTC RFC 3339 timestamp ending in Z")
        try:
            datetime.fromisoformat(self.created_at[:-1] + "+00:00")
        except ValueError as exc:
            raise _fail("created_at is not a valid timestamp") from exc

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "intent_id": self.intent_id,
            "agent_id": self.agent_id,
            "repository_path": self.repository_path,
            "repository_id": self.repository_id,
            "source_ref": self.source_ref,
            "target_ref": self.target_ref,
            "base_oid": self.base_oid,
            "source_oid": self.source_oid,
            "target_oid": self.target_oid,
            "summary": self.summary,
            "dependency_intent_ids": list(self.dependency_intent_ids),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "IntegrationIntent":
        if not isinstance(payload, dict):
            raise _fail("top level must be an object")
        if set(payload) != _FIELDS:
            missing = sorted(_FIELDS - set(payload))
            unknown = sorted(set(payload) - _FIELDS)
            raise _fail(f"fields do not match schema (missing={missing}, unknown={unknown})")
        dependencies = payload["dependency_intent_ids"]
        if type(dependencies) is not list:
            raise _fail("dependency_intent_ids must be a list")
        return cls(
            schema=payload["schema"],
            intent_id=payload["intent_id"],
            agent_id=payload["agent_id"],
            repository_path=payload["repository_path"],
            repository_id=payload["repository_id"],
            source_ref=payload["source_ref"],
            target_ref=payload["target_ref"],
            base_oid=payload["base_oid"],
            source_oid=payload["source_oid"],
            target_oid=payload["target_oid"],
            summary=payload["summary"],
            dependency_intent_ids=tuple(dependencies),
            created_at=payload["created_at"],
        )


class IntegrationIntentStore:
    """An immutable, deterministic store in VersionDrift's local state directory."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        base = str(base_dir) if base_dir is not None else None
        self.directory = _event_path(base).parent / "integration-intents"

    def _path(self, intent_id: str) -> Path:
        return self.directory / (_intent_id(intent_id) + ".json")

    def _validate_directory(self) -> bool:
        """Return whether the final store path is an existing real directory."""
        try:
            metadata = self.directory.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"integration intent store is a symlink: {self.directory}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"integration intent store is not a directory: {self.directory}")
        return True

    def _open_directory(self) -> int:
        if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
            raise RuntimeError("secure integration intent store access is unavailable")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        return os.open(str(self.directory), flags)

    @staticmethod
    def _open_entry(directory_fd: int, name: str) -> int:
        if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
            raise RuntimeError("secure integration intent entry access is unavailable")
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        entry_fd = os.open(name, flags, dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(entry_fd).st_mode):
                raise ValueError(f"integration intent entry is not a regular file: {name}")
        except BaseException:
            os.close(entry_fd)
            raise
        return entry_fd

    def _load_from_directory(self, directory_fd: int, intent_id: str) -> IntegrationIntent:
        name = _intent_id(intent_id) + ".json"
        try:
            entry_fd = self._open_entry(directory_fd, name)
            try:
                source = os.fdopen(entry_fd, "r", encoding="utf-8")
            except BaseException:
                os.close(entry_fd)
                raise
            with source:
                payload = json.load(source, object_pairs_hook=_unique_object)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            raise _fail(f"cannot load {intent_id}: {exc}") from exc
        intent = IntegrationIntent.from_dict(payload)
        if intent.intent_id != intent_id:
            raise _fail("filename does not match envelope intent_id")
        return intent

    def _create_posix(self, validated: IntegrationIntent) -> Path:
        if not self._validate_directory():
            self.directory.mkdir(parents=True)
            self._validate_directory()
        destination = self._path(validated.intent_id)
        directory_fd = self._open_directory()
        try:
            _store_opened(directory_fd)
            if os.open not in os.supports_dir_fd or os.unlink not in os.supports_dir_fd:
                raise RuntimeError("descriptor-relative intent creation is unavailable")
            if os.link not in os.supports_dir_fd:
                raise RuntimeError("atomic immutable intent publication is unavailable")
            temporary_name = f".{validated.intent_id}.{secrets.token_hex(8)}.tmp"
            destination_name = validated.intent_id + ".json"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0)
            temporary_fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
            try:
                try:
                    data = (
                        json.dumps(
                            validated.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
                        )
                        + "\n"
                    ).encode("utf-8")
                    view = memoryview(data)
                    while view:
                        written = os.write(temporary_fd, view)
                        if written == 0:
                            raise OSError("short write while storing integration intent")
                        view = view[written:]
                    os.fsync(temporary_fd)
                finally:
                    os.close(temporary_fd)
                os.link(
                    temporary_name,
                    destination_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                os.fsync(directory_fd)
            finally:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(directory_fd)
        return destination

    def create(self, intent: IntegrationIntent) -> Path:
        # Re-parse so only a fully validated envelope reaches storage.
        validated = IntegrationIntent.from_dict(intent.to_dict())
        if os.name == "posix":
            return self._create_posix(validated)
        if not self._validate_directory():
            self.directory.mkdir(parents=True)
            self._validate_directory()
        destination = self._path(validated.intent_id)
        if destination.exists():
            raise FileExistsError(destination)
        temporary = self.directory / f".{validated.intent_id}.{secrets.token_hex(8)}.tmp"
        text = json.dumps(validated.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            atomic_write_text(temporary, text)
            os.link(temporary, destination)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return destination

    def load(self, intent_id: str) -> IntegrationIntent:
        _intent_id(intent_id)
        if os.name == "posix":
            self._validate_directory()
            directory_fd = self._open_directory()
            try:
                _store_opened(directory_fd)
                return self._load_from_directory(directory_fd, intent_id)
            finally:
                os.close(directory_fd)
        path = self._path(intent_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            raise _fail(f"cannot load {intent_id}: {exc}") from exc
        intent = IntegrationIntent.from_dict(payload)
        if intent.intent_id != intent_id:
            raise _fail("filename does not match envelope intent_id")
        return intent

    def list(self) -> List[IntegrationIntent]:
        if not self._validate_directory():
            return []
        if os.name == "posix":
            if os.listdir not in os.supports_fd:
                raise RuntimeError("descriptor-relative intent listing is unavailable")
            directory_fd = self._open_directory()
            try:
                _store_opened(directory_fd)
                names = sorted(name for name in os.listdir(directory_fd) if name.endswith(".json"))
                return [self._load_from_directory(directory_fd, name[:-5]) for name in names]
            finally:
                os.close(directory_fd)
        paths = sorted(
            (path for path in self.directory.iterdir() if path.suffix == ".json"),
            key=lambda item: item.name,
        )
        return [self.load(path.stem) for path in paths]
