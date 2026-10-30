"""Entities of both adapter families, commands and failure handling."""

import asyncio
from datetime import timedelta

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
import pytest

from custom_components.tplink_powerline.const import CONFIRM_DELAY, DOMAIN
from custom_components.tplink_powerline.diagnostics import async_get_config_entry_diagnostics

from . import fixtures


def entity_id(hass, platform: str, unique_id: str) -> str:
    found = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert found, f"no {platform} with unique id {unique_id}"
    return found


def unique(mac: str, key: str) -> str:
    return f"{mac}_{key}"


async def test_encrypted_entities(hass, encrypted_entry):
    mac, peer = fixtures.ENCRYPTED_MAC, fixtures.LEGACY_MAC
    rx = hass.states.get(entity_id(hass, "sensor", f"{mac}_{peer}_rx"))
    tx = hass.states.get(entity_id(hass, "sensor", f"{mac}_{peer}_tx"))
    assert (rx.state, tx.state) == ("105", "103")
    assert rx.attributes["unit_of_measurement"] == "Mbit/s"
    assert rx.attributes["friendly_name"] == f"Garage Receive rate from {peer.upper()}"
    assert hass.states.get(entity_id(hass, "sensor", unique(mac, "adapters"))).state == "1"
    assert hass.states.get(entity_id(hass, "sensor", unique(mac, "wifi_clients"))).state == "1"
    assert (
        hass.states.get(entity_id(hass, "binary_sensor", unique(mac, "powerline"))).state
        == STATE_ON
    )
    ethernet = hass.states.get(entity_id(hass, "binary_sensor", f"{mac}_ethernet_0"))
    assert ethernet.state == STATE_OFF
    assert ethernet.attributes["friendly_name"] == "Garage Ethernet"
    assert hass.states.get(entity_id(hass, "switch", unique(mac, "led"))).state == STATE_ON
    assert hass.states.get(entity_id(hass, "switch", unique(mac, "wifi_2g"))).state == STATE_ON
    assert hass.states.get(entity_id(hass, "switch", unique(mac, "wifi_5g"))).state == STATE_OFF
    assert entity_id(hass, "button", unique(mac, "restart"))


async def test_legacy_has_no_wifi_entities(hass, legacy_entry):
    mac = fixtures.LEGACY_MAC
    registry = er.async_get(hass)
    keys = {
        e.unique_id.removeprefix(f"{mac}_")
        for e in er.async_entries_for_config_entry(registry, legacy_entry.entry_id)
    }
    assert keys == {
        "adapters",
        "powerline",
        "led",
        "restart",
        f"{fixtures.ENCRYPTED_MAC}_rx",
        f"{fixtures.ENCRYPTED_MAC}_tx",
    }
    rx = hass.states.get(entity_id(hass, "sensor", f"{mac}_{fixtures.ENCRYPTED_MAC}_rx"))
    assert rx.state == "103"


async def test_led_switch(hass, encrypted_entry, encrypted_device, advance):
    led = entity_id(hass, "switch", unique(fixtures.ENCRYPTED_MAC, "led"))
    await hass.services.async_call("switch", "turn_off", {"entity_id": led}, blocking=True)
    assert hass.states.get(led).state == STATE_OFF
    assert encrypted_device.writes[-1][1] == {"enable": "toggle", "toggle": "off"}
    await hass.services.async_call("switch", "turn_on", {"entity_id": led}, blocking=True)
    assert hass.states.get(led).state == STATE_ON
    # Confirmed by a re-read shortly after the command: here the adapter says otherwise
    logins = encrypted_device.logins
    encrypted_device.data["/admin/ledSettings?form=enable"]["enable"] = "0"
    await advance(timedelta(seconds=CONFIRM_DELAY + 1))
    assert encrypted_device.logins == logins + 1
    assert hass.states.get(led).state == STATE_OFF


async def test_wifi_switch_and_restart(hass, encrypted_entry, encrypted_device):
    mac = fixtures.ENCRYPTED_MAC
    wifi = entity_id(hass, "switch", unique(mac, "wifi_2g"))
    await hass.services.async_call("switch", "turn_off", {"entity_id": wifi}, blocking=True)
    assert hass.states.get(wifi).state == STATE_OFF
    path, written = encrypted_device.writes[-1]
    assert path == "/admin/wireless?form=wireless_2g"
    assert written["enable"] == "off"
    restart = entity_id(hass, "button", unique(mac, "restart"))
    await hass.services.async_call("button", "press", {"entity_id": restart}, blocking=True)
    assert encrypted_device.writes[-1] == ("/admin/reboot.json", {})


async def test_legacy_led_switch(hass, legacy_entry, legacy_device):
    led = entity_id(hass, "switch", unique(fixtures.LEGACY_MAC, "led"))
    await hass.services.async_call("switch", "turn_off", {"entity_id": led}, blocking=True)
    assert legacy_device.writes == [{"TPLINK.LEDSCHEDULE.LED_ENABLE": "NO"}]
    assert hass.states.get(led).state == STATE_OFF


async def test_command_failure_raises(hass, encrypted_entry, encrypted_device):
    encrypted_device.fail_with = "busy"
    led = entity_id(hass, "switch", unique(fixtures.ENCRYPTED_MAC, "led"))
    with pytest.raises(HomeAssistantError, match="busy"):
        await hass.services.async_call("switch", "turn_off", {"entity_id": led}, blocking=True)
    assert hass.states.get(led).state == STATE_ON


async def test_peer_appears_and_disappears(hass, legacy_entry, legacy_device, next_poll):
    mac, other = fixtures.LEGACY_MAC, "02:00:00:00:00:d4"
    legacy_device.data["DIDMNG.GENERAL.MACS"] += f",{other}"
    legacy_device.data["DIDMNG.GENERAL.RX_BPS"] += ",640"
    legacy_device.data["DIDMNG.GENERAL.TX_BPS"] += ",960"
    await next_poll()
    assert hass.states.get(entity_id(hass, "sensor", f"{mac}_{other}_tx")).state == "30"
    assert hass.states.get(entity_id(hass, "sensor", unique(mac, "adapters"))).state == "2"
    legacy_device.data["DIDMNG.GENERAL.MACS"] = "00:00:00:00:00:00,02:00:00:00:00:a1"
    legacy_device.data["DIDMNG.GENERAL.RX_BPS"] = legacy_device.data["DIDMNG.GENERAL.TX_BPS"] = (
        "0,0"
    )
    await next_poll()
    assert (
        hass.states.get(entity_id(hass, "sensor", f"{mac}_{other}_tx")).state == STATE_UNAVAILABLE
    )
    link = entity_id(hass, "binary_sensor", unique(mac, "powerline"))
    assert hass.states.get(link).state == STATE_OFF


async def test_unreachable_then_back(hass, encrypted_entry, encrypted_device, next_poll):
    led = entity_id(hass, "switch", unique(fixtures.ENCRYPTED_MAC, "led"))
    encrypted_device.fail_with = "timeout"
    await next_poll()
    assert hass.states.get(led).state == STATE_UNAVAILABLE
    encrypted_device.fail_with = None
    await next_poll()
    assert hass.states.get(led).state == STATE_ON


async def test_password_changed_starts_reauth_and_stops_polling(
    hass, encrypted_entry, encrypted_device, next_poll
):
    encrypted_device.password = "changed"
    await next_poll()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
    failures = encrypted_device.failures
    await next_poll()
    await next_poll()
    # No more attempts with the old password: each one counts towards the lockout
    assert encrypted_device.failures == failures


async def test_diagnostics_hide_secrets(hass, encrypted_entry):
    diagnostics = await async_get_config_entry_diagnostics(hass, encrypted_entry)
    text = str(diagnostics)
    for secret in (fixtures.PASSWORD, "02:00:00:00:00:c3", "192.0.2.50", "wifi-secret"):
        assert secret not in text
    assert fixtures.ENCRYPTED_HOST not in text
    assert diagnostics["family"] == "encrypted"
    assert diagnostics["state"]["peers"] == [{"rx_rate": 105, "tx_rate": 103}]


async def test_forced_refresh_never_retries_a_rejected_password(
    hass, encrypted_entry, encrypted_device, next_poll
):
    await async_setup_component(hass, "homeassistant", {})
    encrypted_device.password = "changed"
    await next_poll()
    failures = encrypted_device.failures
    led = entity_id(hass, "switch", unique(fixtures.ENCRYPTED_MAC, "led"))
    for _ in range(3):
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": led}, blocking=True
        )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert encrypted_device.failures == failures


async def test_command_with_rejected_password_starts_reauth(
    hass, encrypted_entry, encrypted_device, next_poll
):
    encrypted_device.password = "changed"
    led = entity_id(hass, "switch", unique(fixtures.ENCRYPTED_MAC, "led"))
    with pytest.raises(HomeAssistantError, match="rejected the password"):
        await hass.services.async_call("switch", "turn_off", {"entity_id": led}, blocking=True)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
    await next_poll()
    # The entity is now unavailable: Home Assistant does not even run the command
    assert hass.states.get(led).state == STATE_UNAVAILABLE
    await hass.services.async_call("switch", "turn_on", {"entity_id": led}, blocking=True)
    # One wrong attempt in total: neither the poll nor the second command logged in
    assert encrypted_device.failures == 1


async def test_offline_peer_keeps_its_sensors_after_restart(hass, legacy_entry, legacy_device):
    rx = entity_id(hass, "sensor", f"{fixtures.LEGACY_MAC}_{fixtures.ENCRYPTED_MAC}_rx")
    legacy_device.data["DIDMNG.GENERAL.MACS"] = "00:00:00:00:00:00,02:00:00:00:00:a1"
    legacy_device.data["DIDMNG.GENERAL.RX_BPS"] = "0,0"
    legacy_device.data["DIDMNG.GENERAL.TX_BPS"] = "0,0"
    assert await hass.config_entries.async_reload(legacy_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(rx).state == STATE_UNAVAILABLE


async def test_restart_button_when_the_adapter_drops_the_answer(
    hass, encrypted_entry, encrypted_device
):
    encrypted_device.drop_reboot_answer = True
    restart = entity_id(hass, "button", unique(fixtures.ENCRYPTED_MAC, "restart"))
    await hass.services.async_call("button", "press", {"entity_id": restart}, blocking=True)
    assert encrypted_device.writes == [("/admin/reboot.json", {})]


async def test_rejection_survives_a_restart(hass, encrypted_entry, encrypted_device, next_poll):
    encrypted_device.password = "changed"
    await next_poll()
    assert encrypted_entry.data["rejected"] is True
    failures = encrypted_device.failures
    # A restart (reload) must not try the rejected password again
    await hass.config_entries.async_reload(encrypted_entry.entry_id)
    await hass.async_block_till_done()
    assert encrypted_device.failures == failures
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == SOURCE_REAUTH
    result = await hass.config_entries.flow.async_configure(
        flows[0]["flow_id"], {"password": "changed"}
    )
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert encrypted_entry.data["rejected"] is False
    assert encrypted_entry.state is ConfigEntryState.LOADED


async def test_wifi_entities_appear_when_the_adapter_answers(
    hass, encrypted_device, encrypted_entry_factory, next_poll
):
    encrypted_device.missing = {"/admin/wlan_status", "/admin/wireless?form=statistics"}
    entry = await encrypted_entry_factory()
    registry = er.async_get(hass)
    wifi = unique(fixtures.ENCRYPTED_MAC, "wifi_2g")
    assert registry.async_get_entity_id("switch", DOMAIN, wifi) is None
    encrypted_device.missing = set()
    await next_poll()
    assert hass.states.get(entity_id(hass, "switch", wifi)).state == STATE_ON
    clients = entity_id(hass, "sensor", unique(fixtures.ENCRYPTED_MAC, "wifi_clients"))
    assert hass.states.get(clients).state == "1"
    # Missing again: unknown, not zero
    encrypted_device.missing = {"/admin/wireless?form=statistics"}
    await next_poll()
    assert hass.states.get(clients).state == STATE_UNAVAILABLE
    assert entry.state is ConfigEntryState.LOADED


async def test_queued_command_does_not_retry_a_rejected_password(
    hass, encrypted_entry, encrypted_device
):
    encrypted_device.password = "changed"
    coordinator = encrypted_entry.runtime_data
    led = entity_id(hass, "switch", unique(fixtures.ENCRYPTED_MAC, "led"))
    await coordinator.client.lock.acquire()
    try:
        poll = hass.async_create_task(coordinator.async_refresh())
        command = hass.async_create_task(
            hass.services.async_call("switch", "turn_off", {"entity_id": led}, blocking=True)
        )
        for _ in range(20):
            await asyncio.sleep(0)
    finally:
        coordinator.client.lock.release()
    await poll
    with pytest.raises(HomeAssistantError, match="rejected the password"):
        await command
    assert encrypted_device.failures == 1


async def test_command_refused_while_the_password_is_rejected(
    hass, encrypted_entry, encrypted_device
):
    coordinator = encrypted_entry.runtime_data
    coordinator._reject()
    with pytest.raises(HomeAssistantError, match="rejected the password"):
        await coordinator.async_command(lambda: coordinator.client.set_led(False), led=False)
    assert encrypted_device.writes == []
