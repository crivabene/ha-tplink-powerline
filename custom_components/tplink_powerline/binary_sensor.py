"""Powerline link and Ethernet port connectivity."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PowerlineConfigEntry
from .coordinator import PowerlineCoordinator, PowerlineEntity, add_entities_as_they_appear

# Read-only, served by the coordinator
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerlineConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([PowerlineLinkSensor(coordinator)])

    def candidates() -> dict:
        count = len(coordinator.data.ethernet or [])
        return {
            f"ethernet_{index}": lambda index=index: EthernetSensor(coordinator, index, count)
            for index in range(count)
        }

    add_entities_as_they_appear(entry, coordinator, async_add_entities, candidates)


class PowerlineLinkSensor(PowerlineEntity, BinarySensorEntity):
    """On while at least one other adapter is reachable over the mains."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: PowerlineCoordinator) -> None:
        super().__init__(coordinator, "powerline")

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.peers)


class EthernetSensor(PowerlineEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: PowerlineCoordinator, index: int, ports: int) -> None:
        super().__init__(coordinator, "ethernet" if ports == 1 else "ethernet_port")
        self._index = index
        self._attr_unique_id = f"{coordinator.data.mac}_ethernet_{index}"
        if ports > 1:
            self._attr_translation_placeholders = {"port": str(index + 1)}

    @property
    def port(self) -> dict | None:
        ports = self.coordinator.data.ethernet or []
        return ports[self._index] if self._index < len(ports) else None

    @property
    def available(self) -> bool:
        return super().available and self.port is not None

    @property
    def is_on(self) -> bool | None:
        port = self.port
        return None if port is None else port["connected"]

    @property
    def extra_state_attributes(self) -> dict | None:
        port = self.port
        if not port or not port["connected"]:
            return None
        return {"speed": port["speed"], "duplex": port["duplex"]}
