"""Polling coordinator and base entity.

The adapters accept one admin session at a time and lock the web UI after about ten
wrong passwords: a rejected password stops polling at once and asks for a new one, and
stays rejected across restarts until re-authentication or reconfiguration clears it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import PowerlineAuthError, PowerlineClient, PowerlineError, PowerlineState
from .const import (
    CONF_REJECTED,
    CONF_WEBSITE_CHECK,
    CONFIRM_DELAY,
    DOMAIN,
    FIRMWARE_INTERVAL,
    POLL_INTERVAL,
)
from .website import latest_firmware, page_url

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FirmwareCheck:
    """The latest firmware, from the adapter itself or from TP-Link's download page."""

    latest: str | None
    up_to_date: bool
    # The adapter can download and install it itself (its firmware page's online upgrade)
    installable: bool
    notes: str | None = None
    url: str | None = None


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
        self.firmware = FirmwareCoordinator(hass, entry, self)

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
            state = await self.client.state()
        except PowerlineAuthError as err:
            self._reject()
            raise ConfigEntryAuthFailed(str(err)) from err
        except PowerlineError as err:
            raise UpdateFailed(str(err)) from err
        if self.data is not None and (state.firmware, state.hardware) != (
            self.data.firmware,
            self.data.hardware,
        ):
            self._update_device(state)
        return state

    def _update_device(self, state: PowerlineState) -> None:
        """New firmware (an upgrade, from here or elsewhere): shown on the device page too."""
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(registry, self.config_entry.entry_id):
            if (DOMAIN, state.mac) in device.identifiers:
                registry.async_update_device(
                    device.id, sw_version=state.firmware, hw_version=state.hardware
                )

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
    ) -> Any:
        """Run a command; `expected` fields are shown at once, then confirmed by a re-read.
        Returns what the command returned."""
        if self.auth_failed:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="auth_failed")
        try:
            result = await action()
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
        if confirm:
            if self._cancel_confirm:
                self._cancel_confirm()
            self._cancel_confirm = async_call_later(self.hass, CONFIRM_DELAY, self._confirm)
        return result

    async def _confirm(self, _now) -> None:
        self._cancel_confirm = None
        await self.async_request_refresh()

    async def async_shutdown(self) -> None:
        if self._cancel_confirm:
            self._cancel_confirm()
            self._cancel_confirm = None
        await self.firmware.async_shutdown()
        await super().async_shutdown()


class FirmwareCoordinator(DataUpdateCoordinator[FirmwareCheck | None]):
    """Checks for new firmware twice a day: with the adapter's own online upgrade check
    when its firmware has one, otherwise on TP-Link's download page if the user turned
    that on. None when there is no way to know."""

    def __init__(self, hass, entry, main: PowerlineCoordinator) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"TP-Link powerline {entry.data[CONF_HOST]} firmware",
            update_interval=FIRMWARE_INTERVAL,
        )
        self.main = main

    async def _async_update_data(self) -> FirmwareCheck | None:
        main = self.main
        if main.auth_failed:
            # Re-authentication is already asked for by the adapter's own polling
            raise UpdateFailed("password rejected, waiting for re-authentication")
        state = main.data
        if state is None:
            raise UpdateFailed("the adapter has not been read yet")
        url = page_url(state.model, state.hardware)
        try:
            info = await main.client.firmware_info(state.firmware)
            if info is not None:
                return FirmwareCheck(
                    latest=info.latest,
                    up_to_date=info.up_to_date,
                    installable=True,
                    notes=info.notes,
                    url=url,
                )
            if not self.config_entry.options.get(CONF_WEBSITE_CHECK):
                return None
            found = await latest_firmware(
                async_get_clientsession(self.hass), state.model, state.hardware
            )
        except PowerlineAuthError as err:
            main._reject()
            raise ConfigEntryAuthFailed(str(err)) from err
        except PowerlineError as err:
            raise UpdateFailed(str(err)) from err
        if found is None:
            return None
        return FirmwareCheck(
            latest=found.version, up_to_date=False, installable=False, url=found.url
        )


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
