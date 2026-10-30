"""Config flow against the fake adapters."""

import asyncio
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.tplink_powerline.api import FAMILY_ENCRYPTED, FAMILY_LEGACY
from custom_components.tplink_powerline.const import DOMAIN

from . import fixtures


async def _start(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_user_flow_detects_each_family(hass, encrypted_device, legacy_device):
    for host, family, title, mac in (
        (fixtures.ENCRYPTED_HOST, FAMILY_ENCRYPTED, "PGW2440 (Garage)", fixtures.ENCRYPTED_MAC),
        (fixtures.LEGACY_HOST, FAMILY_LEGACY, "PG2400P (Study)", fixtures.LEGACY_MAC),
    ):
        result = await _start(hass)
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": host, "password": fixtures.PASSWORD}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == title
        assert result["data"] == {"host": host, "password": fixtures.PASSWORD, "family": family}
        assert result["result"].unique_id == mac


async def test_user_flow_wrong_password_is_tried_once(hass, encrypted_device):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": fixtures.ENCRYPTED_HOST, "password": "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert encrypted_device.failures == 1


async def test_user_flow_cannot_connect(hass, aioclient_mock):
    aioclient_mock.post("http://192.0.2.9/admin/locale?form=index_lang", text="<html>")
    aioclient_mock.post("http://192.0.2.9/", text="<html>")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.0.2.9", "password": "x"}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_already_configured_updates_host(hass, legacy_entry, legacy_device, serve):
    # Same adapter (same MAC), now at another address
    serve(legacy_device, "192.0.2.22")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "http://192.0.2.22/", "password": fixtures.PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert legacy_entry.data["host"] == "192.0.2.22"


async def test_host_is_stored_bare(hass, encrypted_device):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"host": f" http://{fixtures.ENCRYPTED_HOST}/ ", "password": fixtures.PASSWORD},
    )
    assert result["data"]["host"] == fixtures.ENCRYPTED_HOST


async def test_rejected_password_is_not_offered_again(hass, encrypted_device):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": fixtures.ENCRYPTED_HOST, "password": "wrong"}
    )
    suggested = {
        str(key): (key.description or {}).get("suggested_value")
        for key in result["data_schema"].schema
    }
    assert suggested == {"host": fixtures.ENCRYPTED_HOST, "password": None}


async def test_malformed_answer_and_unexpected_error(hass, encrypted_device):
    del encrypted_device.data["/admin/ghn_powerline?form=plc_settings"]["mac"]
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": fixtures.ENCRYPTED_HOST, "password": fixtures.PASSWORD}
    )
    assert result["errors"] == {"base": "cannot_connect"}
    with patch("custom_components.tplink_powerline.config_flow.detect", side_effect=RuntimeError):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": fixtures.ENCRYPTED_HOST, "password": fixtures.PASSWORD}
        )
    assert result["errors"] == {"base": "unknown"}


async def test_flow_waits_for_the_running_poll(hass, encrypted_entry):
    """A flow for a loaded adapter takes its session lock: one admin session at a time."""
    lock = encrypted_entry.runtime_data.client.lock
    result = await encrypted_entry.start_reconfigure_flow(hass)
    await lock.acquire()
    try:
        task = hass.async_create_task(
            hass.config_entries.flow.async_configure(
                result["flow_id"],
                {"host": fixtures.ENCRYPTED_HOST, "password": fixtures.PASSWORD},
            )
        )
        for _ in range(20):  # the clock is frozen: let the flow run as far as it can
            await asyncio.sleep(0)
        assert not task.done()
    finally:
        lock.release()
    result = await task
    assert result["reason"] == "reconfigure_successful"


async def test_reauth(hass, encrypted_entry, encrypted_device):
    encrypted_device.password = "new-password"
    result = await encrypted_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "still-wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "new-password"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert encrypted_entry.data["password"] == "new-password"


async def test_reconfigure_rejects_another_adapter(hass, encrypted_entry, legacy_device):
    result = await encrypted_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": fixtures.LEGACY_HOST, "password": fixtures.PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"


async def test_reconfigure_same_adapter(hass, encrypted_entry, encrypted_device):
    result = await encrypted_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": fixtures.ENCRYPTED_HOST, "password": fixtures.PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"


async def test_reconfigure_wrong_password(hass, encrypted_entry, encrypted_device):
    result = await encrypted_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": fixtures.ENCRYPTED_HOST, "password": "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reconfigure_new_address(hass, encrypted_entry, encrypted_device, serve):
    serve(encrypted_device, "192.0.2.33")
    result = await encrypted_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.0.2.33", "password": fixtures.PASSWORD}
    )
    assert result["reason"] == "reconfigure_successful"
    assert encrypted_entry.data["host"] == "192.0.2.33"


async def test_busy_and_locked_errors(hass, encrypted_device, legacy_device):
    legacy_device.token = "someone-else"
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": fixtures.LEGACY_HOST, "password": fixtures.PASSWORD}
    )
    assert result["errors"] == {"base": "busy"}
    encrypted_device.login_error = "exceeded max attempts"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": fixtures.ENCRYPTED_HOST, "password": fixtures.PASSWORD}
    )
    assert result["errors"] == {"base": "locked"}


async def test_known_host_aborts_before_logging_in(hass, encrypted_entry, encrypted_device):
    logins = encrypted_device.logins
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": fixtures.ENCRYPTED_HOST, "password": "anything"}
    )
    assert result["reason"] == "already_configured"
    assert (encrypted_device.logins, encrypted_device.failures) == (logins, 0)


async def test_reconfigure_pauses_the_waiting_poll(
    hass, encrypted_entry, encrypted_device, next_poll
):
    """A poll queued behind the flow must not log in with the old password afterwards."""
    encrypted_device.password = "new-password"
    lock = encrypted_entry.runtime_data.client.lock
    await lock.acquire()
    flow = await encrypted_entry.start_reconfigure_flow(hass)
    try:
        poll = hass.async_create_task(encrypted_entry.runtime_data.async_refresh())
        configure = hass.async_create_task(
            hass.config_entries.flow.async_configure(
                flow["flow_id"],
                {"host": fixtures.ENCRYPTED_HOST, "password": "new-password"},
            )
        )
        for _ in range(20):
            await asyncio.sleep(0)
    finally:
        lock.release()
    await poll
    result = await configure
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert encrypted_device.failures == 0


async def test_wrong_device_leaves_the_entry_polling(
    hass, encrypted_entry, encrypted_device, legacy_device, next_poll
):
    result = await encrypted_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": fixtures.LEGACY_HOST, "password": fixtures.PASSWORD}
    )
    assert result["reason"] == "wrong_device"
    logins = encrypted_device.logins
    await next_poll()
    assert encrypted_device.logins == logins + 1
    assert encrypted_entry.runtime_data.last_update_success


async def test_readding_a_rejected_adapter_reloads_it(
    hass, encrypted_entry, encrypted_device, serve, next_poll
):
    encrypted_device.password = "changed"
    await next_poll()
    await hass.config_entries.async_reload(encrypted_entry.entry_id)
    await hass.async_block_till_done()
    assert encrypted_entry.state is config_entries.ConfigEntryState.SETUP_ERROR
    serve(encrypted_device, "192.0.2.22")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.0.2.22", "password": "changed"}
    )
    assert result["reason"] == "already_configured"
    await hass.async_block_till_done()
    assert encrypted_entry.state is config_entries.ConfigEntryState.LOADED
    assert encrypted_entry.data["rejected"] is False
    assert encrypted_entry.data["host"] == "192.0.2.22"
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
