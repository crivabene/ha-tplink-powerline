"""Diagnostics: what the adapter reports, without the password or any address."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import PowerlineConfigEntry

TO_REDACT = {CONF_HOST, CONF_PASSWORD, "mac", "ip", "name"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PowerlineConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    state = asdict(coordinator.data) if coordinator.data else None
    if state:
        # Model and peer count are what matters for support; addresses are not needed
        state["peers"] = [
            {"rx_rate": peer["rx_rate"], "tx_rate": peer["tx_rate"]} for peer in state["peers"]
        ]
    firmware = coordinator.firmware
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "family": coordinator.client.family,
        "state": async_redact_data(state, TO_REDACT),
        "last_update_success": coordinator.last_update_success,
        "firmware_check": asdict(firmware.data) if firmware.data else None,
        "firmware_check_success": firmware.last_update_success,
    }
