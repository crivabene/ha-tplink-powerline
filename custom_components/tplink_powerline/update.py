"""Firmware: the adapter's own online upgrade where its firmware has one, otherwise (if
turned on) the latest firmware listed on TP-Link's download page, as a notice only."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PowerlineConfigEntry
from .api import parse_version, short_version
from .const import DOMAIN
from .coordinator import (
    FirmwareCheck,
    PowerlineCoordinator,
    PowerlineEntity,
    add_entities_as_they_appear,
)

# One install at a time; the client also serialises every exchange with the adapter
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerlineConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    def candidates() -> dict:
        if coordinator.firmware.data is None:
            return {}
        return {"firmware": lambda: FirmwareUpdate(coordinator)}

    # Keyed on the firmware check: the entity appears with its first answer
    add_entities_as_they_appear(entry, coordinator.firmware, async_add_entities, candidates)


class FirmwareUpdate(PowerlineEntity, UpdateEntity):
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PowerlineCoordinator) -> None:
        super().__init__(coordinator, "firmware")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.firmware.async_add_listener(self._handle_coordinator_update)
        )

    @property
    def check(self) -> FirmwareCheck | None:
        return self.coordinator.firmware.data

    @property
    def available(self) -> bool:
        if self.in_progress:
            # Writing and restarting: the adapter is offline, the install is not
            return True
        return (
            super().available
            and self.coordinator.firmware.last_update_success
            and self.check is not None
        )

    async def async_update(self) -> None:
        """ "Check for updates" (homeassistant.update_entity): the firmware check, not the
        adapter's state, run now (not debounced) so the call returns with its answer."""
        if not self.enabled:
            return
        await self.coordinator.firmware.async_refresh()

    @property
    def supported_features(self) -> UpdateEntityFeature:
        check = self.check
        if check is None or not check.installable:
            return UpdateEntityFeature(0)
        features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
        if check.notes:
            features |= UpdateEntityFeature.RELEASE_NOTES
        return features

    @property
    def installed_version(self) -> str | None:
        return short_version(self.coordinator.data.firmware)

    @property
    def latest_version(self) -> str | None:
        check = self.check
        if check is None:
            return None
        if check.up_to_date or check.latest is None:
            return self.installed_version
        return short_version(check.latest)

    @property
    def release_url(self) -> str | None:
        check = self.check
        return check.url if check else None

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """By version, then build date ("1.1.0 Build 20250710"); any other text that
        differs counts as newer, as the adapter itself said so."""
        latest, installed = parse_version(latest_version), parse_version(installed_version)
        if latest is None or installed is None:
            return latest_version != installed_version
        return latest > installed

    async def async_release_notes(self) -> str | None:
        check = self.check
        return check.notes if check else None

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        check = self.check
        if check is None or not check.installable:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="install_not_supported"
            )
        coordinator = self.coordinator

        @callback
        def progress(percent: int) -> None:
            self._attr_update_percentage = percent
            self.async_write_ha_state()

        # Home Assistant ends the progress when this returns: wait until the adapter is done
        self._attr_in_progress = True
        self._attr_update_percentage = 0
        self.async_write_ha_state()
        try:
            seconds = await coordinator.async_command(
                lambda: coordinator.client.install_firmware(coordinator.data.firmware, progress),
                confirm=False,
            )
            # Downloaded: the adapter now writes the firmware and restarts, offline meanwhile
            self._attr_update_percentage = None
            self.async_write_ha_state()
            await wait_for_adapter(seconds)
        finally:
            self._attr_update_percentage = None
        before = coordinator.data.firmware
        await coordinator.async_request_refresh()
        if coordinator.data.firmware == before:
            # New firmware starts its own check (see the coordinator); otherwise check here
            await coordinator.firmware.async_request_refresh()


async def wait_for_adapter(seconds: int) -> None:
    """While the adapter writes the firmware and restarts."""
    await asyncio.sleep(seconds)
