"""TP-Link G.hn powerline adapters, read and controlled through their local web API."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import client_for
from .const import CONF_FAMILY, CONF_REJECTED
from .coordinator import PowerlineCoordinator

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]

type PowerlineConfigEntry = ConfigEntry[PowerlineCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: PowerlineConfigEntry) -> bool:
    if entry.data.get(CONF_REJECTED):
        # Logging in again with a rejected password would bring the lockout closer
        raise ConfigEntryAuthFailed("password rejected, waiting for re-authentication")
    client = client_for(
        async_get_clientsession(hass),
        entry.data[CONF_FAMILY],
        entry.data[CONF_HOST],
        entry.data[CONF_PASSWORD],
    )
    coordinator = PowerlineCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Not part of the setup: the firmware check may need the Internet, and the update entity
    # appears once it has an answer (then it keeps checking twice a day)
    entry.async_create_background_task(
        hass, coordinator.firmware.async_refresh(), f"{entry.title} firmware check"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PowerlineConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
