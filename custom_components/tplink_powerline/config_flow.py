"""Config flow: address and web UI password; the firmware family is detected.

Each attempt with a wrong password counts towards the adapter's lockout, so a failed
login is reported at once and never retried on its own.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import (
    PowerlineAuthError,
    PowerlineBusyError,
    PowerlineClient,
    PowerlineError,
    PowerlineLockedError,
    PowerlineState,
    detect,
    normalize_host,
)
from .const import CONF_FAMILY, CONF_REJECTED, CONF_WEBSITE_CHECK, DOMAIN

_LOGGER = logging.getLogger(__name__)

CREDENTIALS = vol.Schema({vol.Required(CONF_HOST): str, vol.Required(CONF_PASSWORD): str})
PASSWORD = vol.Schema({vol.Required(CONF_PASSWORD): str})
OPTIONS = vol.Schema({vol.Required(CONF_WEBSITE_CHECK, default=False): bool})


class PowerlineConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PowerlineOptionsFlow:
        return PowerlineOptionsFlow()

    @staticmethod
    def _pause(entry: ConfigEntry) -> PowerlineClient | None:
        """Pause the entry's client while new settings are tried: a poll waiting for the
        adapter must not log in with the old password once the flow releases it."""
        if entry.state is not ConfigEntryState.LOADED:
            return None
        client = entry.runtime_data.client
        client.paused = True
        return client

    async def _connect(
        self, host: str, password: str
    ) -> tuple[str | None, str | None, PowerlineState | None]:
        """(error, family, state) for this adapter and password."""
        try:
            client = await detect(async_get_clientsession(self.hass), host, password)
            state = await client.check()
        except PowerlineLockedError:
            error = "locked"
        except PowerlineAuthError:
            error = "invalid_auth"
        except PowerlineBusyError:
            error = "busy"
        except PowerlineError as err:
            _LOGGER.debug("Cannot use the adapter at %s: %s", host, err)
            error = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error with the adapter at %s", host)
            error = "unknown"
        else:
            return None, client.family, state
        return error, None, None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        error = None
        if user_input is not None:
            host = normalize_host(user_input[CONF_HOST])
            # Before logging in: each login is an admin session, or a wrong attempt
            self._async_abort_entries_match({CONF_HOST: host})
            error, family, state = await self._connect(host, user_input[CONF_PASSWORD])
            if state is not None:
                if existing := await self.async_set_unique_id(state.mac):
                    return self._update_existing(existing, host, user_input[CONF_PASSWORD])
                return self.async_create_entry(
                    title=f"{state.model} ({state.name})" if state.name else state.model,
                    data={
                        CONF_HOST: host,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_FAMILY: family,
                    },
                )
        return self.async_show_form(
            step_id="user",
            # Never offer a rejected password again
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS, {CONF_HOST: user_input[CONF_HOST]} if user_input else None
            ),
            errors={"base": error} if error else {},
        )

    def _update_existing(self, entry: ConfigEntry, host: str, password: str) -> ConfigFlowResult:
        """Same adapter at a new address: take the address and the password that just worked,
        even if the entry was stopped by a rejected password, and reload it."""
        # The old client goes with the reload: it must not log in again meanwhile
        self._pause(entry)
        self.hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_HOST: host, CONF_PASSWORD: password, CONF_REJECTED: False},
        )
        for flow in self.hass.config_entries.flow.async_progress_by_handler(
            DOMAIN, match_context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id}
        ):
            self.hass.config_entries.flow.async_abort(flow["flow_id"])
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_abort(reason="already_configured")

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        error = None
        if user_input is not None:
            paused, reloading = self._pause(entry), False
            try:
                error, _, state = await self._connect(
                    entry.data[CONF_HOST], user_input[CONF_PASSWORD]
                )
                if state is not None:
                    await self.async_set_unique_id(state.mac)
                    self._abort_if_unique_id_mismatch(reason="wrong_device")
                    reloading = True
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                            CONF_REJECTED: False,
                        },
                    )
            finally:
                # Kept paused only when a new client replaces it
                if paused is not None and not reloading:
                    paused.paused = False
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=PASSWORD,
            description_placeholders={"host": entry.data[CONF_HOST]},
            errors={"base": error} if error else {},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        error = None
        if user_input is not None:
            host = normalize_host(user_input[CONF_HOST])
            paused, reloading = self._pause(entry), False
            try:
                error, family, state = await self._connect(host, user_input[CONF_PASSWORD])
                if state is not None:
                    await self.async_set_unique_id(state.mac)
                    self._abort_if_unique_id_mismatch(reason="wrong_device")
                    reloading = True
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_HOST: host,
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                            CONF_FAMILY: family,
                            CONF_REJECTED: False,
                        },
                    )
            finally:
                # Kept paused only when a new client replaces it
                if paused is not None and not reloading:
                    paused.paused = False
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS,
                {CONF_HOST: user_input[CONF_HOST] if user_input else entry.data[CONF_HOST]},
            ),
            errors={"base": error} if error else {},
        )


class PowerlineOptionsFlow(OptionsFlowWithReload):
    """Whether to look up new firmware on TP-Link's website (reloads the entry)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS, self.config_entry.options),
        )
