"""Polling coordinator and base entity.

The adapters accept one admin session at a time and lock the web UI after about ten
wrong passwords: a rejected password stops polling at once and asks for a new one, and
stays rejected across restarts until re-authentication or reconfiguration clears it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import PowerlineAuthError, PowerlineClient, PowerlineError, PowerlineState
from .const import CONF_REJECTED, CONFIRM_DELAY, DOMAIN, POLL_INTERVAL

_LOGGER = logging.getLogger(__name__)


class PowerlineCoordinator(DataUpdateCoordinator[PowerlineState]):
    """Polls one adapter and runs its commands."""

    def __init__(self, hass, entry, client: PowerlineClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"TP-Link powerline {entry.data[CONF_HOST]}",
            update_interval=POLL_INTERVAL,
        )
        self.client = client
        self._cancel_confirm = None

    @property
    def auth_failed(self) -> bool:
        """Nothing logs in while set, whatever asks (update_entity, automations, commands)."""
        return bool(self.config_entry.data.get(CONF_REJECTED))

    def _reject(self) -> None:
        """Remember the rejection in the entry, so a restart does not try the password again."""
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={**self.config_entry.data, CONF_REJECTED: True}
        )

    async def _async_update_data(self) -> PowerlineState:
        if self.auth_failed:
            raise ConfigEntryAuthFailed("password rejected, waiting for re-authentication")
        try:
            return await self.client.state()
        except PowerlineAuthError as err:
            self._reject()
            raise ConfigEntryAuthFailed(str(err)) from err
        except PowerlineError as err:
            raise UpdateFailed(str(err)) from err

    @property
    def device_info(self) -> DeviceInfo:
        state = self.data
        return DeviceInfo(
            identifiers={(DOMAIN, state.mac)},
            name=state.name or state.model,
            manufacturer="TP-Link",
            model=state.model,
            hw_version=state.hardware,
            sw_version=state.firmware,
            configuration_url=f"http://{self.config_entry.data[CONF_HOST]}",
        )

    async def async_command(
        self, action: Callable[[], Awaitable[Any]], confirm: bool = True, **expected: Any
    ) -> None:
        """Run a command; `expected` fields are shown at once, then confirmed by a re-read."""
        if self.auth_failed:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="auth_failed")
        try:
            await action()
        except PowerlineAuthError as err:
            # A service call cannot start re-authentication by raising: start it here
            self._reject()
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except PowerlineError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        if expected and self.last_update_success:
            # Not on a failing coordinator: that would mark stale data as fresh
            self.async_set_updated_data(replace(self.data, **expected))
        if not confirm:
            return
        if self._cancel_confirm:
            self._cancel_confirm()
        self._cancel_confirm = async_call_later(self.hass, CONFIRM_DELAY, self._confirm)

    async def _confirm(self, _now) -> None:
        self._cancel_confirm = None
        await self.async_request_refresh()

    async def async_shutdown(self) -> None:
        if self._cancel_confirm:
            self._cancel_confirm()
            self._cancel_confirm = None
        await super().async_shutdown()


def add_entities_as_they_appear(
    entry: ConfigEntry,
    coordinator: PowerlineCoordinator,
    async_add_entities: Callable[[list[Entity]], None],
    candidates: Callable[[], dict[str, Callable[[], Entity]]],
) -> None:
    """Add entities now and whenever a poll brings data for new ones (keyed by `candidates`).

    A peer joining the network, or a Wi-Fi answer missing at startup, gets its entities
    at the next poll instead of after a restart.
    """
    added: set[str] = set()

    @callback
    def add() -> None:
        new = {key: build for key, build in candidates().items() if key not in added}
        added.update(new)
        if new:
            async_add_entities([build() for build in new.values()])

    add()
    entry.async_on_unload(coordinator.async_add_listener(add))


class PowerlineEntity(CoordinatorEntity[PowerlineCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: PowerlineCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.data.mac}_{key}"
        self._attr_device_info = coordinator.device_info
