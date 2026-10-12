"""Data models for NEXLORE tasks."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from homeassistant.util import dt as dt_util


@dataclass(frozen=True, slots=True)
class NexloreTask:
    """A task returned by the NEXLORE API."""

    path: str
    line: int
    raw: str
    text: str
    status: str
    file_hash: str
    due: date | datetime | None = None
    completed: datetime | None = None
    uid: str = ""

    @classmethod
    def from_api(cls, value: Any) -> NexloreTask:
        """Build a task from an API item, rejecting incomplete locators."""
        if not isinstance(value, dict):
            raise ValueError("NEXLORE returned a task with an invalid shape")

        path = value.get("path")
        line = value.get("line")
        raw = value.get("raw")
        text = value.get("text")
        status = value.get("status")
        file_hash = value.get("file_hash")

        if (
            not isinstance(path, str)
            or not isinstance(line, int)
            or isinstance(line, bool)
            or not isinstance(raw, str)
            or not isinstance(text, str)
            or status not in {"open", "done", "cancelled"}
            or not isinstance(file_hash, str)
        ):
            raise ValueError("NEXLORE returned a task without a usable locator")

        return cls(
            path=path,
            line=line,
            raw=raw,
            text=text,
            status=status,
            file_hash=file_hash,
            due=_parse_due(value.get("due")),
            completed=_parse_completed(value.get("completed")),
        )

    def with_uid(self, uid: str) -> NexloreTask:
        """Return a copy carrying its Home Assistant item UID."""
        return replace(self, uid=uid)


def _parse_due(value: Any) -> date | datetime | None:
    """Parse NEXLORE's date-only due value without inventing a time."""
    if not isinstance(value, str):
        return None
    parsed_datetime = dt_util.parse_datetime(value)
    if parsed_datetime is not None:
        return parsed_datetime
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_completed(value: Any) -> datetime | None:
    """Return a completion timestamp only when NEXLORE supplies a time."""
    if not isinstance(value, str):
        return None
    return dt_util.parse_datetime(value)
