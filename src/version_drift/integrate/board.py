"""Strictly read-only integration board evaluation over local Git refs."""
from __future__ import annotations

import heapq
import os
import re
import subprocess
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .intent import IntegrationIntent, IntegrationIntentStore

BOARD_SCHEMA = "version-drift/integration-board/1"
_OID = re.compile(r"[0-9a-f]{40}\Z")


class BoardStatus(str, Enum):
    READY = "READY"
    STALE = "STALE"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class ReasonCode(str, Enum):
    SOURCE_OID_CHANGED = "SOURCE_OID_CHANGED"
    TARGET_OID_CHANGED = "TARGET_OID_CHANGED"
    SOURCE_REF_UNOBSERVABLE = "SOURCE_REF_UNOBSERVABLE"
    TARGET_REF_UNOBSERVABLE = "TARGET_REF_UNOBSERVABLE"
    REPOSITORY_MISMATCH = "REPOSITORY_MISMATCH"
    TARGET_MISMATCH = "TARGET_MISMATCH"
    DUPLICATE_SOURCE_REF = "DUPLICATE_SOURCE_REF"
    MISSING_DEPENDENCY = "MISSING_DEPENDENCY"
    DEPENDENCY_OUTSIDE_BOARD = "DEPENDENCY_OUTSIDE_BOARD"
    DEPENDENCY_NOT_READY = "DEPENDENCY_NOT_READY"
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
    MALFORMED_INTENT_STORE = "MALFORMED_INTENT_STORE"
    MALFORMED_INTENT_SET = "MALFORMED_INTENT_SET"


@dataclass(frozen=True)
class BoardItem:
    intent_id: str
    status: BoardStatus
    reason: Optional[ReasonCode]
    source_ref: str
    target_ref: str
    source_oid: Optional[str]
    target_oid: Optional[str]
    dependency_intent_ids: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "status": self.status.value,
            "reason": self.reason.value if self.reason is not None else None,
            "source_ref": self.source_ref,
            "target_ref": self.target_ref,
            "source_oid": self.source_oid,
            "target_oid": self.target_oid,
            "dependency_intent_ids": list(self.dependency_intent_ids),
        }


@dataclass(frozen=True)
class BoardResult:
    schema: str
    status: BoardStatus
    reason: Optional[ReasonCode]
    repository_path: str
    repository_id: str
    target_ref: str
    items: Tuple[BoardItem, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status.value,
            "reason": self.reason.value if self.reason is not None else None,
            "repository_path": self.repository_path,
            "repository_id": self.repository_id,
            "target_ref": self.target_ref,
            "items": [item.to_dict() for item in self.items],
        }


class IntegrationBoard:
    """Evaluate immutable intents against local refs without changing Git state."""

    def __init__(self, repository_path: Path, repository_id: str, target_ref: str) -> None:
        self.repository_path = str(Path(repository_path).resolve())
        self.repository_id = repository_id
        self.target_ref = target_ref

    def _resolve(self, ref: str) -> Optional[str]:
        env = os.environ.copy()
        env["GIT_OPTIONAL_LOCKS"] = "0"
        try:
            completed = subprocess.run(
                ["git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"],
                cwd=self.repository_path,
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError:
            return None
        output = completed.stdout.strip()
        if completed.returncode != 0 or _OID.fullmatch(output) is None:
            return None
        return output

    def inspect_store(self, store: IntegrationIntentStore) -> BoardResult:
        try:
            intents = store.list()
        except (OSError, ValueError):
            return self._failure(ReasonCode.MALFORMED_INTENT_STORE)
        return self.inspect(intents)

    def _failure(self, reason: ReasonCode) -> BoardResult:
        return BoardResult(
            BOARD_SCHEMA, BoardStatus.UNKNOWN, reason, self.repository_path,
            self.repository_id, self.target_ref, (),
        )

    def inspect(self, intents: Iterable[IntegrationIntent]) -> BoardResult:
        values = list(intents)
        if any(not isinstance(value, IntegrationIntent) for value in values):
            return self._failure(ReasonCode.MALFORMED_INTENT_SET)
        by_id = {value.intent_id: value for value in values}
        if len(by_id) != len(values):
            return self._failure(ReasonCode.MALFORMED_INTENT_SET)

        items: Dict[str, BoardItem] = {}
        scoped: Set[str] = set()
        for intent in sorted(values, key=lambda value: value.intent_id):
            if (str(Path(intent.repository_path).resolve()) != self.repository_path
                    or intent.repository_id != self.repository_id):
                items[intent.intent_id] = self._item(
                    intent, BoardStatus.BLOCKED, ReasonCode.REPOSITORY_MISMATCH, None, None)
                continue
            if intent.target_ref != self.target_ref:
                items[intent.intent_id] = self._item(
                    intent, BoardStatus.BLOCKED, ReasonCode.TARGET_MISMATCH, None, None)
                continue
            scoped.add(intent.intent_id)
            source_oid = self._resolve(intent.source_ref)
            target_oid = self._resolve(intent.target_ref)
            if source_oid is None:
                state = (BoardStatus.UNKNOWN, ReasonCode.SOURCE_REF_UNOBSERVABLE)
            elif target_oid is None:
                state = (BoardStatus.UNKNOWN, ReasonCode.TARGET_REF_UNOBSERVABLE)
            elif source_oid != intent.source_oid:
                state = (BoardStatus.STALE, ReasonCode.SOURCE_OID_CHANGED)
            elif target_oid != intent.target_oid:
                state = (BoardStatus.STALE, ReasonCode.TARGET_OID_CHANGED)
            else:
                state = (BoardStatus.READY, None)
            items[intent.intent_id] = self._item(intent, state[0], state[1], source_oid, target_oid)

        candidates = {identifier for identifier in scoped if items[identifier].status is BoardStatus.READY}
        for identifier in sorted(tuple(candidates)):
            intent = by_id[identifier]
            missing = [dep for dep in intent.dependency_intent_ids if dep not in by_id]
            outside = [dep for dep in intent.dependency_intent_ids if dep in by_id and dep not in scoped]
            nonready = [dep for dep in intent.dependency_intent_ids if dep in scoped and dep not in candidates]
            if missing:
                items[identifier] = replace(items[identifier], status=BoardStatus.BLOCKED,
                                            reason=ReasonCode.MISSING_DEPENDENCY)
            elif outside:
                items[identifier] = replace(items[identifier], status=BoardStatus.BLOCKED,
                                            reason=ReasonCode.DEPENDENCY_OUTSIDE_BOARD)
            elif nonready:
                items[identifier] = replace(items[identifier], status=BoardStatus.BLOCKED,
                                            reason=ReasonCode.DEPENDENCY_NOT_READY)

        self._propagate_nonready(items, by_id, scoped)
        active = {identifier for identifier in scoped if items[identifier].status is BoardStatus.READY}
        cycles = self._cycle_nodes(active, by_id)
        for identifier in cycles:
            items[identifier] = replace(items[identifier], status=BoardStatus.BLOCKED,
                                        reason=ReasonCode.DEPENDENCY_CYCLE)
        self._propagate_nonready(items, by_id, scoped)

        # Ownership conflicts apply only to intents that would otherwise be ready.
        claims: Dict[str, List[str]] = {}
        for identifier in scoped:
            if items[identifier].status is BoardStatus.READY:
                claims.setdefault(by_id[identifier].source_ref, []).append(identifier)
        for owners in claims.values():
            if len(owners) > 1:
                for identifier in owners:
                    items[identifier] = replace(items[identifier], status=BoardStatus.BLOCKED,
                                                reason=ReasonCode.DUPLICATE_SOURCE_REF)
        self._propagate_nonready(items, by_id, scoped)

        active = {identifier for identifier in scoped if items[identifier].status is BoardStatus.READY}
        ordered = self._topological(active, by_id)
        ordered.extend(sorted(set(items) - set(ordered)))
        result_items = tuple(items[identifier] for identifier in ordered)
        overall = BoardStatus.READY
        if any(item.status is BoardStatus.UNKNOWN for item in result_items):
            overall = BoardStatus.UNKNOWN
        elif any(item.status is BoardStatus.BLOCKED for item in result_items):
            overall = BoardStatus.BLOCKED
        elif any(item.status is BoardStatus.STALE for item in result_items):
            overall = BoardStatus.STALE
        return BoardResult(
            BOARD_SCHEMA, overall, None, self.repository_path, self.repository_id,
            self.target_ref, result_items,
        )

    @staticmethod
    def _item(intent: IntegrationIntent, status: BoardStatus, reason: Optional[ReasonCode],
              source_oid: Optional[str], target_oid: Optional[str]) -> BoardItem:
        return BoardItem(intent.intent_id, status, reason, intent.source_ref, intent.target_ref,
                         source_oid, target_oid, intent.dependency_intent_ids)

    @staticmethod
    def _propagate_nonready(items: Dict[str, BoardItem], by_id: Dict[str, IntegrationIntent],
                            scoped: Set[str]) -> None:
        changed = True
        while changed:
            changed = False
            for identifier in sorted(scoped):
                if items[identifier].status is not BoardStatus.READY:
                    continue
                if any(dep in scoped and items[dep].status is not BoardStatus.READY
                       for dep in by_id[identifier].dependency_intent_ids):
                    items[identifier] = replace(items[identifier], status=BoardStatus.BLOCKED,
                                                reason=ReasonCode.DEPENDENCY_NOT_READY)
                    changed = True

    @staticmethod
    def _cycle_nodes(active: Set[str], by_id: Dict[str, IntegrationIntent]) -> Set[str]:
        index = 0
        indices: Dict[str, int] = {}
        low: Dict[str, int] = {}
        stack: List[str] = []
        on_stack: Set[str] = set()
        cycles: Set[str] = set()

        def visit(node: str) -> None:
            nonlocal index
            indices[node] = low[node] = index
            index += 1
            stack.append(node)
            on_stack.add(node)
            for dependency in sorted(by_id[node].dependency_intent_ids):
                if dependency not in active:
                    continue
                if dependency not in indices:
                    visit(dependency)
                    low[node] = min(low[node], low[dependency])
                elif dependency in on_stack:
                    low[node] = min(low[node], indices[dependency])
            if low[node] == indices[node]:
                component: List[str] = []
                while True:
                    member = stack.pop()
                    on_stack.remove(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    cycles.update(component)

        for identifier in sorted(active):
            if identifier not in indices:
                visit(identifier)
        return cycles

    @staticmethod
    def _topological(active: Set[str], by_id: Dict[str, IntegrationIntent]) -> List[str]:
        indegree = {identifier: 0 for identifier in active}
        dependents: Dict[str, List[str]] = {identifier: [] for identifier in active}
        for identifier in active:
            for dependency in by_id[identifier].dependency_intent_ids:
                if dependency in active:
                    indegree[identifier] += 1
                    dependents[dependency].append(identifier)
        heap = [identifier for identifier, degree in indegree.items() if degree == 0]
        heapq.heapify(heap)
        result: List[str] = []
        while heap:
            identifier = heapq.heappop(heap)
            result.append(identifier)
            for dependent in sorted(dependents[identifier]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    heapq.heappush(heap, dependent)
        return result
