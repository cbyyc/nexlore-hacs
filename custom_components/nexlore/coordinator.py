"""Polling coordinator and persistent NEXLORE task identity mapping."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from collections import Counter, defaultdict
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import (
    NexloreApiClient,
    NexloreApiError,
    NexloreAuthError,
    NexloreConnectionError,
    NexloreInvalidResponse,
)
from .const import CONF_SPACE, DEFAULT_SCAN_INTERVAL, DOMAIN
from .models import NexloreTask

_LOGGER = logging.getLogger(__name__)
_CHECKBOX = re.compile(r"^(\s*[-*+]\s+\[)[ xX](\])")
_COMPLETION_MARKER = re.compile(r"\s+✅\s+\d{4}-\d{2}-\d{2}\s*$")
_INBOX_TIME = re.compile(r"\s+\(\d{2}:\d{2}\)$")


def _digest(value: str) -> str:
    """Return a stable one-way fingerprint."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_digest(raw: str) -> str:
    """Fingerprint task text while ignoring only completion state and date."""
    normalized = _CHECKBOX.sub(r"\g<1>?\g<2>", raw, count=1)
    normalized = _COMPLETION_MARKER.sub("", normalized).strip()
    return _digest(normalized)


def _identity_record(task: NexloreTask) -> dict[str, Any]:
    """Store locator fingerprints without copying task text into storage."""
    return {
        "path": task.path,
        "line": task.line,
        "raw_hash": _digest(task.raw),
        "canonical_hash": _canonical_digest(task.raw),
        "file_hash": task.file_hash,
        "status": task.status,
    }


class NexloreDataUpdateCoordinator(DataUpdateCoordinator[list[NexloreTask]]):
    """Fetch tasks and keep stable Home Assistant item UIDs where identity is clear."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: NexloreApiClient,
        storage: Store,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {entry.data[CONF_SPACE]}",
            update_interval=DEFAULT_SCAN_INTERVAL,
            config_entry=entry,
            always_update=False,
        )
        self.client = client
        self.space = entry.data[CONF_SPACE]
        self._storage = storage
        self._identity_lock = asyncio.Lock()
        self._task_identities: dict[str, dict[str, Any]] = {}
        self._pending_creates: list[dict[str, Any]] = []
        self._identity_hints: dict[str, dict[str, Any]] = {}
        self._all_tasks: list[NexloreTask] = []
        self._uid_to_task: dict[str, NexloreTask] = {}
        self._last_saved_payload: dict[str, Any] | None = None

    async def async_load_storage(self) -> None:
        """Load previously assigned item UIDs and unresolved create intents."""
        stored = await self._storage.async_load()
        if not isinstance(stored, dict):
            return

        tasks = stored.get("tasks", {})
        if isinstance(tasks, dict):
            self._task_identities = {
                uid: record
                for uid, record in tasks.items()
                if isinstance(uid, str)
                and isinstance(record, dict)
                and isinstance(record.get("path"), str)
                and isinstance(record.get("line"), int)
                and isinstance(record.get("raw_hash"), str)
                and isinstance(record.get("canonical_hash"), str)
                and isinstance(record.get("file_hash"), str)
                and record.get("status") in {"open", "done", "cancelled"}
            }

        pending = stored.get("pending_creates", [])
        if isinstance(pending, list):
            self._pending_creates = [
                item
                for item in pending
                if isinstance(item, dict)
                and isinstance(item.get("uid"), str)
                and isinstance(item.get("summary_hash"), str)
                and isinstance(item.get("before"), list)
            ]

        hints = stored.get("identity_hints", {})
        if isinstance(hints, dict):
            self._identity_hints = {
                uid: hint
                for uid, hint in hints.items()
                if isinstance(uid, str)
                and isinstance(hint, dict)
                and isinstance(hint.get("path"), str)
                and isinstance(hint.get("canonical_hash"), str)
                and hint.get("status") in {"open", "done"}
                and isinstance(hint.get("line"), int)
            }

        self._last_saved_payload = self._storage_payload()

    async def _async_update_data(self) -> list[NexloreTask]:
        """Poll NEXLORE and reconcile tasks against persisted UIDs."""
        try:
            response_items = await self.client.async_get_tasks(self.space)
            tasks = [NexloreTask.from_api(item) for item in response_items]
        except NexloreAuthError as err:
            raise ConfigEntryAuthFailed("NEXLORE rejected the configured token") from err
        except NexloreConnectionError as err:
            raise UpdateFailed("Unable to communicate with NEXLORE") from err
        except (NexloreApiError, NexloreInvalidResponse, ValueError) as err:
            raise UpdateFailed("NEXLORE returned an unusable task response") from err

        async with self._identity_lock:
            self._all_tasks = self._reconcile_tasks(tasks)
            self._uid_to_task = {task.uid: task for task in self._all_tasks}
            await self._save_storage_locked()

        return [task for task in self._all_tasks if task.status != "cancelled"]

    def task_for_uid(self, uid: str) -> NexloreTask | None:
        """Return the latest task for a Home Assistant UID."""
        return self._uid_to_task.get(uid)

    async def async_has_pending_create(self, summary: str) -> bool:
        """Check whether an earlier create with this summary is unresolved."""
        summary_hash = _digest(summary.strip())
        async with self._identity_lock:
            return any(
                pending["summary_hash"] == summary_hash
                for pending in self._pending_creates
            )

    async def async_begin_create(self, summary: str) -> str:
        """Persist intent and a baseline before making a non-idempotent API call."""
        summary_hash = _digest(summary.strip())
        async with self._identity_lock:
            if any(
                pending["summary_hash"] == summary_hash
                for pending in self._pending_creates
            ):
                raise ValueError("A previous create with this summary is unresolved")

            before_counts = Counter(
                (task.path, _digest(task.raw))
                for task in self._all_tasks
                if self._is_inbox_path(task.path)
            )
            uid = str(uuid.uuid4())
            self._pending_creates.append(
                {
                    "uid": uid,
                    "summary_hash": summary_hash,
                    "expected_path": None,
                    "before": [
                        {"path": path, "raw_hash": raw_hash, "count": count}
                        for (path, raw_hash), count in before_counts.items()
                    ],
                }
            )
            await self._save_storage_locked()
            return uid

    async def async_set_pending_path(self, uid: str, path: str | None) -> None:
        """Narrow an acknowledged inbox write to the returned note path."""
        if not isinstance(path, str):
            return
        async with self._identity_lock:
            for pending in self._pending_creates:
                if pending["uid"] == uid:
                    pending["expected_path"] = path
                    await self._save_storage_locked()
                    return

    async def async_cancel_pending_create(self, uid: str) -> None:
        """Remove an intent only when the API definitely rejected the write."""
        async with self._identity_lock:
            previous_count = len(self._pending_creates)
            self._pending_creates = [
                pending for pending in self._pending_creates if pending["uid"] != uid
            ]
            if len(self._pending_creates) != previous_count:
                await self._save_storage_locked()

    async def async_add_identity_hint(
        self,
        uid: str,
        previous: NexloreTask,
        desired_status: str,
        occurrence_added: bool,
    ) -> None:
        """Prefer the completed/reopened occurrence after a successful write."""
        async with self._identity_lock:
            self._identity_hints[uid] = {
                "path": previous.path,
                "canonical_hash": _canonical_digest(previous.raw),
                "status": desired_status,
                "line": previous.line + 1 if occurrence_added else previous.line,
            }
            await self._save_storage_locked()

    def _reconcile_tasks(self, tasks: list[NexloreTask]) -> list[NexloreTask]:
        """Reuse UIDs only when current locator evidence identifies one task."""
        assigned: dict[int, str] = {}
        used_uids: set[str] = set()
        unmatched_indexes = set(range(len(tasks)))

        # The inbox endpoint prepends its new task. Compare a saved multiset
        # baseline so an uncertain POST is only accepted after one unique delta.
        current_groups: dict[tuple[str, str], list[int]] = defaultdict(list)
        for index, task in enumerate(tasks):
            if self._is_inbox_path(task.path):
                current_groups[(task.path, _digest(task.raw))].append(index)

        resolved_pending: set[str] = set()
        for pending in self._pending_creates:
            before_counts = {
                (item["path"], item["raw_hash"]): item["count"]
                for item in pending["before"]
                if isinstance(item, dict)
                and isinstance(item.get("path"), str)
                and isinstance(item.get("raw_hash"), str)
                and isinstance(item.get("count"), int)
            }
            candidates: list[int] = []
            expected_path = pending.get("expected_path")
            for (path, raw_hash), indexes in current_groups.items():
                if expected_path is not None and path != expected_path:
                    continue
                delta = len(indexes) - before_counts.get((path, raw_hash), 0)
                if delta <= 0:
                    continue
                newest_first = sorted(indexes, key=lambda index: tasks[index].line)[:delta]
                candidates.extend(
                    index
                    for index in newest_first
                    if tasks[index].status == "open"
                    and _digest(_INBOX_TIME.sub("", tasks[index].text.strip()))
                    == pending["summary_hash"]
                )

            candidates = [
                index
                for index in candidates
                if index in unmatched_indexes and index not in assigned
            ]
            if len(candidates) == 1:
                index = candidates[0]
                task = tasks[index]
                previous_uids = [
                    uid
                    for uid, record in self._task_identities.items()
                    if record["path"] == task.path
                    and record["line"] == task.line
                    and record["raw_hash"] == _digest(task.raw)
                    and uid not in used_uids
                ]
                uid = previous_uids[0] if len(previous_uids) == 1 else pending["uid"]
                assigned[index] = uid
                used_uids.add(uid)
                unmatched_indexes.remove(index)
                resolved_pending.add(pending["uid"])

        blocked_uids: set[str] = set()
        for uid, hint in self._identity_hints.items():
            candidates = [
                index
                for index in unmatched_indexes
                if tasks[index].path == hint["path"]
                and _canonical_digest(tasks[index].raw) == hint["canonical_hash"]
                and tasks[index].status == hint["status"]
            ]
            at_expected_line = [
                index for index in candidates if tasks[index].line == hint["line"]
            ]
            if len(at_expected_line) == 1:
                index = at_expected_line[0]
                assigned[index] = uid
                used_uids.add(uid)
                unmatched_indexes.remove(index)
            elif len(candidates) == 1:
                index = candidates[0]
                assigned[index] = uid
                used_uids.add(uid)
                unmatched_indexes.remove(index)
            else:
                blocked_uids.add(uid)
        self._identity_hints.clear()

        old_records = {
            uid: record
            for uid, record in self._task_identities.items()
            if uid not in used_uids and uid not in blocked_uids
        }

        def match_unique(old_key: Any, new_key: Any) -> None:
            old_groups: dict[Any, list[str]] = defaultdict(list)
            new_groups: dict[Any, list[int]] = defaultdict(list)
            for uid, record in old_records.items():
                if uid not in used_uids:
                    old_groups[old_key(record)].append(uid)
            for index in unmatched_indexes:
                new_groups[new_key(tasks[index])].append(index)

            for key, old_uids in old_groups.items():
                new_indexes = new_groups.get(key, [])
                if len(old_uids) == 1 and len(new_indexes) == 1:
                    uid = old_uids[0]
                    index = new_indexes[0]
                    if uid not in used_uids and index in unmatched_indexes:
                        assigned[index] = uid
                        used_uids.add(uid)
                        unmatched_indexes.remove(index)

        match_unique(
            lambda record: (record["path"], record["line"], record["raw_hash"]),
            lambda task: (task.path, task.line, _digest(task.raw)),
        )
        match_unique(
            lambda record: (record["path"], record["raw_hash"]),
            lambda task: (task.path, _digest(task.raw)),
        )
        match_unique(
            lambda record: (record["path"], record["canonical_hash"]),
            lambda task: (task.path, _canonical_digest(task.raw)),
        )

        for index in unmatched_indexes:
            uid = str(uuid.uuid4())
            assigned[index] = uid
            used_uids.add(uid)

        mapped = [task.with_uid(assigned[index]) for index, task in enumerate(tasks)]
        self._task_identities = {
            task.uid: _identity_record(task)
            for task in mapped
        }
        self._pending_creates = [
            pending
            for pending in self._pending_creates
            if pending["uid"] not in resolved_pending
        ]
        return mapped

    def _is_inbox_path(self, path: str) -> bool:
        """Recognize NEXLORE's documented top-level inbox notes."""
        parent, separator, filename = path.rpartition("/")
        return (
            bool(separator)
            and parent == self.space
            and filename.casefold() in {"inbox.md", "eingang.md"}
        )

    def _storage_payload(self) -> dict[str, Any]:
        """Return the integration's persistent identity state."""
        return {
            "tasks": self._task_identities,
            "pending_creates": self._pending_creates,
            "identity_hints": self._identity_hints,
        }

    async def _save_storage_locked(self) -> None:
        """Persist only when identity state changes."""
        payload = self._storage_payload()
        if payload != self._last_saved_payload:
            await self._storage.async_save(payload)
            self._last_saved_payload = payload
