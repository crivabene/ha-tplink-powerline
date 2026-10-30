"""Link rates to every other adapter, and counters."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfDataRate
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PowerlineConfigEntry
from .api import Peer
from .coordinator import PowerlineCoordinator, PowerlineEntity, add_entities_as_they_appear

# Read-only, served by the coordinator
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerlineConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([AdapterCountSensor(coordinator)])

    # A peer missing at startup is left to Home Assistant: its sensors show as unavailable
    # ("no longer provided"), and can be deleted if the adapter is gone for good
    def candidates() -> dict:
        found = {}
        if coordinator.data.wifi_clients is not None:
            found["wifi_clients"] = lambda: WifiClientsSensor(coordinator)
        for mac in (peer.mac for peer in coordinator.data.peers):
            for direction in ("rx", "tx"):
                found[f"{mac}_{direction}"] = lambda mac=mac, direction=direction: PeerRateSensor(
                    coordinator, mac, direction
                )
        return found

    add_entities_as_they_appear(entry, coordinator, async_add_entities, candidates)


class PeerRateSensor(PowerlineEntity, SensorEntity):
    """Rate this adapter receives from (rx) or sends to (tx) one peer."""

    _attr_device_class = SensorDeviceClass.DATA_RATE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfDataRate.MEGABITS_PER_SECOND
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: PowerlineCoordinator, mac: str, direction: str) -> None:
        super().__init__(coordinator, f"peer_{direction}")
        self._mac = mac
        self._direction = direction
        self._attr_unique_id = f"{coordinator.data.mac}_{mac}_{direction}"
        self._attr_translation_placeholders = {"peer": mac.upper()}

    @property
    def peer(self) -> Peer | None:
        return next((p for p in self.coordinator.data.peers if p.mac == self._mac), None)

    @property
    def available(self) -> bool:
        return super().available and self.peer is not None

    @property
    def native_value(self) -> int | None:
        peer = self.peer
        if peer is None:
            return None
        return peer.rx_rate if self._direction == "rx" else peer.tx_rate


class AdapterCountSensor(PowerlineEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: PowerlineCoordinator) -> None:
        super().__init__(coordinator, "adapters")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.peers)


class WifiClientsSensor(PowerlineEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: PowerlineCoordinator) -> None:
        super().__init__(coordinator, "wifi_clients")

    @property
    def available(self) -> bool:
        # Unknown, not zero, when the adapter did not answer for its clients this time
        return super().available and self.coordinator.data.wifi_clients is not None

    @property
    def native_value(self) -> int | None:
        clients = self.coordinator.data.wifi_clients
        return None if clients is None else len(clients)
