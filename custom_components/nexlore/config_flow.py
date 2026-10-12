"""Config flow for NEXLORE."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import aiohttp_client, selector

from .api import (
    NexloreApiClient,
    NexloreApiError,
    NexloreAuthError,
    NexloreConnectionError,
    NexloreInvalidResponse,
)
from .const import CONF_API_TOKEN, CONF_BASE_URL, CONF_SPACE, DOMAIN


def _writable_spaces(spaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only spaces where a write-level token can mutate tasks."""
    return [
        space
        for space in spaces
        if isinstance(space.get("name"), str)
        and space.get("role") in {"write", "manage"}
    ]


class NexloreConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure a NEXLORE task list."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Ask for the API origin and token, then discover spaces."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                base_url = NexloreApiClient.normalize_base_url(user_input[CONF_BASE_URL])
            except (KeyError, ValueError):
                errors["base"] = "invalid_url"
            else:
                token = user_input[CONF_API_TOKEN].strip()
                client = NexloreApiClient(
                    aiohttp_client.async_get_clientsession(self.hass),
                    base_url,
                    token,
                )
                try:
                    profile, spaces = await client.async_get_profile_and_spaces()
                    if profile.get("level") != "write":
                        errors["base"] = "read_only_token"
                    else:
                        self._spaces = _writable_spaces(spaces)
                        if not self._spaces:
                            errors["base"] = "no_writable_spaces"
                        else:
                            self._flow_data = {
                                CONF_BASE_URL: base_url,
                                CONF_API_TOKEN: token,
                            }
                            return await self.async_step_space()
                except NexloreAuthError:
                    errors["base"] = "invalid_auth"
                except (NexloreConnectionError, NexloreApiError, NexloreInvalidResponse):
                    errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BASE_URL): str,
                    vol.Required(CONF_API_TOKEN): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD,
                            autocomplete="current-password",
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_space(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Let the user select one writable NEXLORE space."""
        if user_input is not None:
            space = user_input[CONF_SPACE]
            if space not in {value["name"] for value in self._spaces}:
                return self.async_abort(reason="invalid_space")

            unique_id = f"{self._flow_data[CONF_BASE_URL]}|{space}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=f"NEXLORE {space}",
                data={**self._flow_data, CONF_SPACE: space},
            )

        options = [
            {"value": value["name"], "label": value["name"]}
            for value in self._spaces
        ]
        return self.async_show_form(
            step_id="space",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SPACE): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Start a flow to replace an expired or revoked token."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Validate a replacement token without changing the selected space."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            token = user_input[CONF_API_TOKEN].strip()
            client = NexloreApiClient(
                aiohttp_client.async_get_clientsession(self.hass),
                entry.data[CONF_BASE_URL],
                token,
            )
            try:
                profile, spaces = await client.async_get_profile_and_spaces()
                writable = _writable_spaces(spaces)
                if profile.get("level") != "write":
                    errors["base"] = "read_only_token"
                elif entry.data[CONF_SPACE] not in {
                    value["name"] for value in writable
                }:
                    errors["base"] = "invalid_space"
                else:
                    await self.async_set_unique_id(entry.unique_id)
                    self._abort_if_unique_id_mismatch()
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={CONF_API_TOKEN: token},
                        reason="reauth_successful",
                    )
            except NexloreAuthError:
                errors["base"] = "invalid_auth"
            except (NexloreConnectionError, NexloreApiError, NexloreInvalidResponse):
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({
                vol.Required(CONF_API_TOKEN): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.PASSWORD,
                        autocomplete="current-password",
                    )
                )
            }),
            errors=errors,
        )
