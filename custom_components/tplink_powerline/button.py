"""Restart button."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PowerlineConfigEntry
from .coordinator import PowerlineCoordinator, PowerlineEntity

# One command at a time; the client also serialises every exchange with the adapter
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerlineConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([RestartButton(entry.runtime_data)])


class RestartButton(PowerlineEntity, ButtonEntity):
    """Restarts the adapter: the powerline link drops for about a minute."""

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PowerlineCoordinator) -> None:
        super().__init__(coordinator, "restart")

    async def async_press(self) -> None:
        # No confirming re-read: the adapter is down for a while, the next poll will tell
        await self.coordinator.async_command(self.coordinator.client.reboot, confirm=False)
