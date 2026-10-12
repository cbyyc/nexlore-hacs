"""NEXLORE custom integration setup."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.storage import Store

from .api import NexloreApiClient
from .const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    DOMAIN,
    PLATFORMS,
    STORAGE_VERSION,
)
from .coordinator import NexloreDataUpdateCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one NEXLORE task list."""
    client = NexloreApiClient(
        aiohttp_client.async_get_clientsession(hass),
        entry.data[CONF_BASE_URL],
        entry.data[CONF_API_TOKEN],
    )
    storage = Store(
        hass,
        STORAGE_VERSION,
        f"{DOMAIN}_{entry.entry_id}_task_identities",
    )
    coordinator = NexloreDataUpdateCoordinator(
        hass,
        entry,
        client,
        storage,
    )
    await coordinator.async_load_storage()
    entry.runtime_data = coordinator
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the to-do platform."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
