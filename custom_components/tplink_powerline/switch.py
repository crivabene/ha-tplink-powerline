"""LEDs and their night mode and, on adapters with Wi-Fi, each band and guest network."""

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
GUEST_BANDS = {"2g": "guest_2g", "5g": "guest_5g"}


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
        if data.night_mode is not None:
            found["night_mode"] = lambda: NightModeSwitch(coordinator)
        for band, field in BANDS.items():
            if getattr(data, field) is not None:
                found[field] = lambda band=band, field=field: WifiSwitch(coordinator, band, field)
        for band, field in GUEST_BANDS.items():
            if getattr(data, field) is not None:
                found[field] = lambda band=band, field=field: GuestSwitch(coordinator, band, field)
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


class NightModeSwitch(PowerlineEntity, SwitchEntity):
    """LEDs off every night on the schedule set in the web UI (shown as attributes)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PowerlineCoordinator) -> None:
        super().__init__(coordinator, "night_mode")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.night_mode is not None

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.night_mode

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        data = self.coordinator.data
        return {"start": data.night_start, "end": data.night_end}

    async def _set(self, on: bool) -> None:
        await self.coordinator.async_command(
            lambda: self.coordinator.client.set_night_mode(on), night_mode=on
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class BandSwitch(PowerlineEntity, SwitchEntity):
    """A setting of one Wi-Fi band, stored in the state field of the same name."""

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

    def _command(self, on: bool):
        raise NotImplementedError

    async def _set(self, on: bool) -> None:
        await self.coordinator.async_command(lambda: self._command(on), **{self._field: on})

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class WifiSwitch(BandSwitch):
    """One Wi-Fi band. Turning it off disconnects every client on that band."""

    def _command(self, on: bool):
        return self.coordinator.client.set_wifi(self._band, on)


class GuestSwitch(BandSwitch):
    """The guest network of one band, as set up in the web UI. Unavailable while the band
    itself is off."""

    def _command(self, on: bool):
        return self.coordinator.client.set_guest(self._band, on)
