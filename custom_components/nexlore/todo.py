"""Home Assistant to-do list platform for NEXLORE."""

from __future__ import annotations

from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .api import (
    NexloreApiError,
    NexloreAuthError,
    NexloreConnectionError,
    NexloreInvalidResponse,
    NexloreTaskChangedError,
)
from .const import CONF_SPACE
from .coordinator import NexloreDataUpdateCoordinator
from .models import NexloreTask


class NexloreTodoList(CoordinatorEntity, TodoListEntity):
    """A NEXLORE space exposed as a standard Home Assistant to-do list."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:format-list-checks"
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
    )

    def __init__(
        self,
        coordinator: NexloreDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_unique_id = entry.entry_id
        self._attr_name = entry.data[CONF_SPACE]

    @property
    def todo_items(self) -> list[TodoItem]:
        """Return the last coordinator snapshot without performing I/O."""
        return [_as_todo_item(task) for task in self.coordinator.data or []]

    async def async_create_todo_item(self, item: TodoItem) -> None:
        """Create one task through NEXLORE's inbox endpoint."""
        summary = (item.summary or "").strip()
        if not summary:
            raise HomeAssistantError("A NEXLORE task needs a title")
        if "\n" in summary or "\r" in summary:
            raise HomeAssistantError("NEXLORE tasks must use a single-line title")
        if item.due is not None or item.description is not None:
            raise HomeAssistantError(
                "NEXLORE task creation currently supports titles only"
            )

        pending_before_refresh = await self.coordinator.async_has_pending_create(summary)
        await self.coordinator.async_request_refresh()
        if pending_before_refresh:
            if await self.coordinator.async_has_pending_create(summary):
                raise HomeAssistantError(
                    "A previous NEXLORE create is still uncertain. Refresh the list and "
                    "confirm it in NEXLORE before trying the same title again."
                )
            return

        try:
            uid = await self.coordinator.async_begin_create(summary)
        except ValueError as err:
            raise HomeAssistantError(
                "A previous NEXLORE create is still uncertain; no duplicate was sent"
            ) from err

        try:
            response = await self.coordinator.client.async_create_inbox_task(
                self.coordinator.space,
                summary,
            )
            await self.coordinator.async_set_pending_path(uid, response.get("path"))
        except NexloreAuthError as err:
            await self.coordinator.async_cancel_pending_create(uid)
            raise ConfigEntryAuthFailed("NEXLORE rejected the configured token") from err
        except NexloreApiError as err:
            if err.status is not None and err.status < 500:
                await self.coordinator.async_cancel_pending_create(uid)
                raise HomeAssistantError(
                    "NEXLORE rejected the task create request"
                ) from err
            # A server error may arrive after the inbox write committed.
        except (NexloreConnectionError, NexloreInvalidResponse):
            # The inbox endpoint has no idempotency key. Reconcile before reporting;
            # this request is never repeated automatically.
            pass

        try:
            await self.coordinator.async_request_refresh()
        except ConfigEntryAuthFailed:
            raise
        except UpdateFailed:
            raise HomeAssistantError(
                "NEXLORE may have accepted this task, but the list could not be "
                "refreshed. The integration will reconcile it later and will not "
                "repeat the create request."
            ) from None

        if self.coordinator.task_for_uid(uid) is None:
            raise HomeAssistantError(
                "NEXLORE may have accepted this task, but it could not be matched "
                "uniquely. The integration will not repeat the create request; "
                "check the NEXLORE inbox before trying again."
            )

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Complete or reopen a task; reject all other edits."""
        if not item.uid:
            raise HomeAssistantError("This NEXLORE task has no stable item ID")

        task = self.coordinator.task_for_uid(item.uid)
        if task is None:
            raise HomeAssistantError(
                "This NEXLORE task is no longer available; refresh the list"
            )

        if task.status == "cancelled":
            raise HomeAssistantError("Cancelled NEXLORE tasks cannot be changed from Home Assistant")

        original_item = _as_todo_item(task)
        if item.summary != original_item.summary:
            raise HomeAssistantError(
                "NEXLORE task titles cannot be edited from Home Assistant"
            )
        if item.due != original_item.due:
            raise HomeAssistantError(
                "NEXLORE due dates cannot be edited from Home Assistant"
            )
        if item.description != original_item.description:
            raise HomeAssistantError(
                "NEXLORE task descriptions cannot be edited from Home Assistant"
            )
        if item.status not in {
            TodoItemStatus.NEEDS_ACTION,
            TodoItemStatus.COMPLETED,
        }:
            raise HomeAssistantError("NEXLORE requires a valid task status")

        desired_status = (
            "done"
            if item.status == TodoItemStatus.COMPLETED
            else "open"
        )
        if task.status == desired_status:
            return

        current = task
        result: dict[str, Any] | None = None
        for attempt in range(2):
            try:
                result = await self._async_write_status(current, desired_status)
                break
            except NexloreTaskChangedError as err:
                if attempt:
                    raise HomeAssistantError(
                        "NEXLORE changed this task again. Refresh the list and retry."
                    ) from err
                try:
                    await self.coordinator.async_request_refresh()
                except ConfigEntryAuthFailed:
                    raise
                except UpdateFailed as refresh_err:
                    raise HomeAssistantError(
                        "NEXLORE changed this task, and the list could not be refreshed"
                    ) from refresh_err
                current = self.coordinator.task_for_uid(item.uid)
                if current is None:
                    raise HomeAssistantError(
                        "NEXLORE changed this task and it could not be identified "
                        "uniquely. Refresh the list and resolve it in NEXLORE."
                    ) from err
                if current.status == desired_status:
                    return
            except NexloreAuthError as err:
                raise ConfigEntryAuthFailed(
                    "NEXLORE rejected the configured token"
                ) from err
            except (NexloreConnectionError, NexloreInvalidResponse) as err:
                await self._async_resolve_uncertain_status(item.uid, desired_status, err)
                return
            except NexloreApiError as err:
                if err.status is not None and err.status >= 500:
                    await self._async_resolve_uncertain_status(
                        item.uid,
                        desired_status,
                        err,
                    )
                    return
                raise HomeAssistantError(
                    "NEXLORE rejected the task status change"
                ) from err

        if result is None:
            raise HomeAssistantError("NEXLORE task status could not be updated")
        if result.get("conflict") is not None:
            raise HomeAssistantError(
                "NEXLORE reported a task conflict. Refresh the list and retry."
            )

        await self.coordinator.async_add_identity_hint(
            item.uid,
            current,
            desired_status,
            occurrence_added=bool(result.get("added")),
        )
        try:
            await self.coordinator.async_request_refresh()
        except ConfigEntryAuthFailed:
            raise
        except UpdateFailed as err:
            raise HomeAssistantError(
                "NEXLORE accepted the status change, but the list refresh failed; "
                "the next poll will reconcile it."
            ) from err

    async def _async_write_status(
        self,
        task: NexloreTask,
        desired_status: str,
    ) -> dict[str, Any]:
        """Write one complete/reopen request using the current API locator."""
        return await self.coordinator.client.async_set_task_status(
            path=task.path,
            line=task.line,
            raw=task.raw,
            file_hash=task.file_hash,
            done=desired_status == "done",
            today=dt_util.now().date().isoformat(),
        )

    async def _async_resolve_uncertain_status(
        self,
        uid: str,
        desired_status: str,
        cause: Exception,
    ) -> None:
        """Refresh after a lost write response without sending a second write."""
        try:
            await self.coordinator.async_request_refresh()
        except ConfigEntryAuthFailed:
            raise
        except UpdateFailed as err:
            raise HomeAssistantError(
                "NEXLORE may have changed this task, but its status could not be "
                "confirmed. Refresh the list before retrying."
            ) from err

        current = self.coordinator.task_for_uid(uid)
        if current is None or current.status != desired_status:
            raise HomeAssistantError(
                "NEXLORE may have changed this task, but its status could not be "
                "confirmed. Refresh the list before retrying; no second write was sent."
            ) from cause


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the to-do entity."""
    coordinator: NexloreDataUpdateCoordinator = entry.runtime_data
    async_add_entities([NexloreTodoList(coordinator, entry)])


def _as_todo_item(task: NexloreTask) -> TodoItem:
    """Convert a NEXLORE task to Home Assistant's standard item model."""
    return TodoItem(
        uid=task.uid,
        summary=task.text,
        status=(
            TodoItemStatus.COMPLETED
            if task.status == "done"
            else TodoItemStatus.NEEDS_ACTION
        ),
        due=task.due,
        completed=task.completed,
    )
