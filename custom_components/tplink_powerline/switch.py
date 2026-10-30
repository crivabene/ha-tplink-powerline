"""LEDs and, on adapters with Wi-Fi, each band."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PowerlineConfigEntry
from .coordinator import PowerlineCoordinator, PowerlineEntity, add_entities_as_they_appear

# One command at a time; the client also serialises every exchange with the adapter
PARALLEL_UPDATES = 1

BANDS = {"2g": "wifi_2g", "5g": "wifi_5g"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerlineConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    def candidates() -> dict:
        data = coordinator.data
        found = {}
        if data.led is not None:
            found["led"] = lambda: LedSwitch(coordinator)
        for band, field in BANDS.items():
            if getattr(data, field) is not None:
                found[field] = lambda band=band, field=field: WifiSwitch(coordinator, band, field)
        return found

    add_entities_as_they_appear(entry, coordinator, async_add_entities, candidates)


class LedSwitch(PowerlineEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PowerlineCoordinator) -> None:
        super().__init__(coordinator, "led")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.led is not None

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.led

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_command(
            lambda: self.coordinator.client.set_led(True), led=True
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_command(
            lambda: self.coordinator.client.set_led(False), led=False
        )


class WifiSwitch(PowerlineEntity, SwitchEntity):
    """One Wi-Fi band. Turning it off disconnects every client on that band."""

    def __init__(self, coordinator: PowerlineCoordinator, band: str, field: str) -> None:
        super().__init__(coordinator, field)
        self._band = band
        self._field = field

    @property
    def available(self) -> bool:
        return super().available and getattr(self.coordinator.data, self._field) is not None

    @property
    def is_on(self) -> bool | None:
        return getattr(self.coordinator.data, self._field)

    async def _set(self, on: bool) -> None:
        await self.coordinator.async_command(
            lambda: self.coordinator.client.set_wifi(self._band, on), **{self._field: on}
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
