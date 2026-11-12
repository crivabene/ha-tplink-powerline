"""LED night mode, guest networks and firmware updates (adapter's own and website check)."""

from datetime import timedelta
from unittest.mock import patch

import aiohttp
from homeassistant.components.update import ATTR_IN_PROGRESS, UpdateEntityFeature
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.tplink_powerline.api import (
    EncryptedClient,
    LegacyClient,
    PowerlineError,
    PowerlineUpgradingError,
    guest_settings,
    parse_version,
    release_notes,
    short_version,
)
from custom_components.tplink_powerline.const import CONF_WEBSITE_CHECK, DOMAIN
from custom_components.tplink_powerline.diagnostics import async_get_config_entry_diagnostics
from custom_components.tplink_powerline.update import FirmwareUpdate
from custom_components.tplink_powerline.website import (
    hardware_major,
    latest_on_page,
    page_url,
)

from . import fixtures

ENC, LEG = fixtures.ENCRYPTED_MAC, fixtures.LEGACY_MAC
LED = "/admin/ledSettings?form=enable"
GUEST_2G = "/admin/guest?form=guest_2g"
PG2400P_PAGE = "https://www.tp-link.com/en/support/download/pg2400p-kit/v1/"
# As the download page lists them (other hardware versions and models included)
PAGE_HTML = """
<a>PG2400P KIT(EU)_V1_1.1.0 Build 20250710</a>
<a>PG2400P(EU)_V1_1.0.3 Build 20221213</a>
<a>PG2400P KIT(EU)_V1_1.0.2 Build 20221101</a>
<a>PG2400P KIT(EU)_V2_2.0.0 Build 20260101</a>
<a>PGW2440 KIT(EU)_V1_9.9.9 Build 20990101</a>
"""


def entity_id(hass, platform: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


async def call(hass, domain: str, service: str, entity: str, **data) -> None:
    await hass.services.async_call(domain, service, {"entity_id": entity} | data, blocking=True)


def cloud_requests(device) -> list[tuple[str, dict]]:
    return [r for r in device.requests if r[0].startswith("/admin/cloud_account")]


# --- LED night mode ---------------------------------------------------------------


async def test_encrypted_night_mode(hass, encrypted_entry, encrypted_device):
    night = entity_id(hass, "switch", f"{ENC}_night_mode")
    state = hass.states.get(night)
    assert state.state == STATE_OFF
    assert state.attributes["start"] == "22:30"
    assert state.attributes["end"] == "06:00"
    assert state.attributes["friendly_name"] == "Garage LED night mode"

    encrypted_device.requests.clear()
    await call(hass, "switch", "turn_on", night)
    assert hass.states.get(night).state == STATE_ON
    # As the LED page: the flag, then the schedule it already holds, unchanged
    assert [(p, r) for p, r in encrypted_device.requests if p == LED] == [
        (LED, {"operation": "load"}),
        (LED, {"operation": "write", "enable": "1"}),
        (LED, {"operation": "update", "shour": "22", "smin": "30", "ehour": "6", "emin": "0"}),
    ]
    encrypted_device.requests.clear()
    await call(hass, "switch", "turn_off", night)
    assert hass.states.get(night).state == STATE_OFF
    writes = [r for p, r in encrypted_device.requests if p == LED and r["operation"] != "load"]
    assert writes == [{"operation": "write", "enable": "0"}]


async def test_legacy_night_mode(hass, legacy_entry, legacy_device):
    night = entity_id(hass, "switch", f"{LEG}_night_mode")
    attributes = hass.states.get(night).attributes
    assert (attributes["start"], attributes["end"]) == ("02:00", "04:00")
    await call(hass, "switch", "turn_on", night)
    assert legacy_device.writes[-1] == {
        "TPLINK.LEDSCHEDULE.ENABLE": "YES",
        "TPLINK.LEDSCHEDULE.START_HOUR": "2",
        "TPLINK.LEDSCHEDULE.START_MINUTE": "0",
        "TPLINK.LEDSCHEDULE.END_HOUR": "4",
        "TPLINK.LEDSCHEDULE.END_MINUTE": "0",
    }
    assert hass.states.get(night).state == STATE_ON
    await call(hass, "switch", "turn_off", night)
    assert legacy_device.writes[-1] == {"TPLINK.LEDSCHEDULE.ENABLE": "NO"}
    assert hass.states.get(night).state == STATE_OFF


async def test_led_settings_run_by_easymesh(
    hass, encrypted_entry_factory, encrypted_device, next_poll
):
    await encrypted_entry_factory()
    led, night = (entity_id(hass, "switch", f"{ENC}_{key}") for key in ("led", "night_mode"))
    encrypted_device.data[LED]["module_enable"] = "off"
    await next_poll()
    # The web UI greys them out: not offered as switches meanwhile
    assert hass.states.get(led).state == STATE_UNAVAILABLE
    assert hass.states.get(night).state == STATE_UNAVAILABLE


async def test_night_mode_missing_on_older_legacy_firmware(hass, serve, freezer):
    device = fixtures.LegacyDevice()
    for key in fixtures.LEGACY_NIGHT_GROUP:
        del device.data[key]
    original = device.__call__

    async def no_night(method, url, data):
        if "TPLINK.LEDSCHEDULE.ENABLE" in url.query_string:
            return fixtures._response(method, url, "ERROR=001\r\n")
        return await original(method, url, data)

    serve(no_night, fixtures.LEGACY_HOST)
    client = LegacyClient(async_get_clientsession(hass), fixtures.LEGACY_HOST, fixtures.PASSWORD)
    client_state = await client.state()
    assert client_state.night_mode is None
    assert client_state.led is True


# --- Guest network ------------------------------------------------------------------


async def test_guest_network(hass, encrypted_entry, encrypted_device):
    guest = entity_id(hass, "switch", f"{ENC}_guest_2g")
    assert hass.states.get(guest).state == STATE_OFF
    assert hass.states.get(guest).attributes["friendly_name"] == "Garage Guest Wi-Fi 2.4 GHz"
    # Greyed out in the web UI while the 5 GHz band is off: no entity yet
    assert entity_id(hass, "switch", f"{ENC}_guest_5g") is None

    await call(hass, "switch", "turn_on", guest)
    assert encrypted_device.writes[-1] == (
        GUEST_2G,
        {
            "enable": "on",
            "ssid": "Home-Guest",
            "hidden": "off",
            "encryption": "psk",
            "psk_key": "guest-secret",
            "psk_version": "auto",
        },
    )
    assert hass.states.get(guest).state == STATE_ON
    await call(hass, "switch", "turn_off", guest)
    assert encrypted_device.writes[-1] == (GUEST_2G, {"enable": "off"})
    assert hass.states.get(guest).state == STATE_OFF


async def test_guest_5g_appears_with_its_band(hass, encrypted_entry, encrypted_device, next_poll):
    encrypted_device.data["/admin/guest?form=guest_5g"]["guest_5g_disabled"] = "off"
    await next_poll()
    guest = entity_id(hass, "switch", f"{ENC}_guest_5g")
    assert hass.states.get(guest).state == STATE_OFF
    encrypted_device.data["/admin/guest?form=guest_5g"]["guest_5g_disabled"] = "on"
    await next_poll()
    assert hass.states.get(guest).state == STATE_UNAVAILABLE


def test_guest_settings_as_the_web_ui():
    current = fixtures.ENCRYPTED_DATA[GUEST_2G] | {"guest_2g_psk_version": "saeAuto"}
    # WPA2/WPA3 is "psk" with the SAE version: written back as the page does
    assert guest_settings(current, "2g", True)["psk_version"] == "saeAuto"
    assert guest_settings(current, "2g", True)["encryption"] == "psk"
    # A version the page cannot show would be changed by writing: refused
    with pytest.raises(PowerlineError, match="security version"):
        guest_settings(current | {"guest_2g_psk_version": "sae"}, "2g", True)
    open_net = fixtures.ENCRYPTED_DATA["/admin/guest?form=guest_5g"]
    assert guest_settings(open_net, "5g", True) == {
        "enable": "on",
        "ssid": "Home-Guest-5G",
        "hidden": "off",
        "encryption": "none",
        "psk_key": "",
        "psk_version": "auto",
    }
    with pytest.raises(PowerlineError, match="security"):
        guest_settings(current | {"guest_2g_encryption": "wep"}, "2g", True)
    with pytest.raises(PowerlineError, match="no name"):
        guest_settings(current | {"guest_2g_ssid": ""}, "2g", True)


# --- Firmware: adapter's own online upgrade (encrypted 1.1.0+) ------------------------


async def test_no_firmware_entity_and_no_cloud_request_before_1_1(
    hass, encrypted_entry, encrypted_device, legacy_entry
):
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entity_id(hass, "update", f"{ENC}_firmware") is None
    assert entity_id(hass, "update", f"{LEG}_firmware") is None
    # Firmware 1.0.4 drops the connection on those endpoints: never asked
    assert cloud_requests(encrypted_device) == []


async def test_online_upgrade(hass, encrypted_entry_factory, encrypted_device, freezer):
    encrypted_device.firmware_1_1()
    entry = await encrypted_entry_factory()
    await hass.async_block_till_done(wait_background_tasks=True)
    firmware = entity_id(hass, "update", f"{ENC}_firmware")
    state = hass.states.get(firmware)
    assert state.state == STATE_ON
    assert state.attributes["installed_version"] == "1.1.0 Build 20250826"
    assert state.attributes["latest_version"] == "1.1.1 Build 20260301"
    assert state.attributes["release_url"] == (
        "https://www.tp-link.com/en/support/download/pgw2440-kit/v1/"
    )
    features = state.attributes["supported_features"]
    assert features & UpdateEntityFeature.INSTALL
    assert features & UpdateEntityFeature.RELEASE_NOTES

    encrypted_device.requests.clear()
    waited: list[tuple[int, bool, object]] = []

    async def adapter_upgrades(seconds: int) -> None:
        # Downloaded: in progress, no percentage, while the adapter writes and restarts
        state = hass.states.get(firmware)
        waited.append(
            (seconds, state.attributes[ATTR_IN_PROGRESS], state.attributes.get("update_percentage"))
        )
        encrypted_device.data["/admin/firmware?form=get_firmware_info"]["firmware_version"] = (
            "1.1.1 Build 20260301 Rel.12345"
        )
        encrypted_device.cloud_data |= {"latest_flag": True}
        freezer.tick(timedelta(seconds=seconds))

    with (
        patch("custom_components.tplink_powerline.api.DOWNLOAD_POLL", 0),
        patch("custom_components.tplink_powerline.update.wait_for_adapter", adapter_upgrades),
    ):
        await call(hass, "update", "install", firmware)
    # upgrade_time + reboot_time as the adapter announced them, plus a margin
    assert waited == [(240, True, None)]
    assert cloud_requests(encrypted_device)[:8] == [
        ("/admin/cloud_account?form=cloud_upgrade", {"operation": "read"}),
        ("/admin/cloud_account?form=check_internet", {"operation": "read"}),
        ("/admin/cloud_account?form=check_device", {"operation": "read"}),
        ("/admin/cloud_account?form=check_connection", {"operation": "read"}),
        ("/admin/cloud_account?form=cloud_upgrade", {"operation": "upgrade"}),
        ("/admin/cloud_account?form=detect_upgrade_status", {"operation": "read"}),
        ("/admin/cloud_account?form=detect_upgrade_status", {"operation": "read"}),
        ("/admin/cloud_account?form=detect_upgrade_status", {"operation": "read"}),
    ]
    # No logout while it writes the firmware: the restart ends that session
    assert encrypted_device.logouts == encrypted_device.logins - 1
    await hass.async_block_till_done()
    state = hass.states.get(firmware)
    assert state.attributes[ATTR_IN_PROGRESS] is False
    assert state.attributes["installed_version"] == "1.1.1 Build 20260301"
    assert state.state == STATE_OFF
    registry = dr.async_get(hass)
    (device,) = dr.async_entries_for_config_entry(registry, entry.entry_id)
    assert device.sw_version == "1.1.1 Build 20260301 Rel.12345"


async def test_online_upgrade_download_fails(hass, encrypted_entry_factory, encrypted_device):
    encrypted_device.firmware_1_1()
    await encrypted_entry_factory()
    await hass.async_block_till_done(wait_background_tasks=True)
    firmware = entity_id(hass, "update", f"{ENC}_firmware")
    encrypted_device.download_fail = True
    with (
        patch("custom_components.tplink_powerline.api.DOWNLOAD_POLL", 0),
        pytest.raises(HomeAssistantError, match="could not download"),
    ):
        await call(hass, "update", "install", firmware)
    assert hass.states.get(firmware).attributes[ATTR_IN_PROGRESS] is False
    assert encrypted_device.logouts == encrypted_device.logins


async def test_online_upgrade_up_to_date(hass, encrypted_entry_factory, encrypted_device):
    encrypted_device.firmware_1_1()
    encrypted_device.cloud_data |= {"latest_flag": True, "latest_version": "", "detail": ""}
    await encrypted_entry_factory()
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(entity_id(hass, "update", f"{ENC}_firmware"))
    assert state.state == STATE_OFF
    assert state.attributes["latest_version"] == "1.1.0 Build 20250826"
    assert not state.attributes["supported_features"] & UpdateEntityFeature.RELEASE_NOTES


async def test_firmware_info_never_asks_older_firmware(hass, encrypted_device):
    client = EncryptedClient(
        async_get_clientsession(hass), fixtures.ENCRYPTED_HOST, fixtures.PASSWORD
    )
    assert await client.firmware_info("1.0.4 Build 20240830 Rel.67970") is None
    assert await client.firmware_info(None) is None
    with pytest.raises(PowerlineError, match="cannot upgrade itself"):
        await client.install_firmware("1.0.4 Build 20240830 Rel.67970", lambda _p: None)
    assert encrypted_device.logins == 0


# --- Firmware: TP-Link website check (option) ---------------------------------------


async def test_website_check_is_off_by_default(hass, legacy_entry, aioclient_mock):
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not [c for c in aioclient_mock.mock_calls if "tp-link.com" in str(c[1])]
    assert entity_id(hass, "update", f"{LEG}_firmware") is None


async def test_website_check(hass, legacy_entry, aioclient_mock):
    aioclient_mock.get(PG2400P_PAGE, text=PAGE_HTML)
    result = await hass.config_entries.options.async_init(legacy_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_WEBSITE_CHECK: True}
    )
    assert legacy_entry.options == {CONF_WEBSITE_CHECK: True}
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(entity_id(hass, "update", f"{LEG}_firmware"))
    assert state.state == STATE_ON
    assert state.attributes["installed_version"] == "1.0.3 Build 20221213"
    assert state.attributes["latest_version"] == "1.1.0 Build 20250710"
    assert state.attributes["release_url"] == PG2400P_PAGE
    # A notice only: installed from the web UI or the tpPLC utility
    assert state.attributes["supported_features"] == 0
    diagnostics = await async_get_config_entry_diagnostics(hass, legacy_entry)
    assert diagnostics["firmware_check"]["latest"] == "1.1.0 Build 20250710"
    assert diagnostics["options"] == {CONF_WEBSITE_CHECK: True}


async def test_website_check_page_down(hass, legacy_entry, aioclient_mock):
    aioclient_mock.get(PG2400P_PAGE, status=503)
    result = await hass.config_entries.options.async_init(legacy_entry.entry_id)
    await hass.config_entries.options.async_configure(result["flow_id"], {CONF_WEBSITE_CHECK: True})
    await hass.async_block_till_done(wait_background_tasks=True)
    # No answer yet: no entity, and the adapter itself is unaffected
    assert entity_id(hass, "update", f"{LEG}_firmware") is None
    assert hass.states.get(entity_id(hass, "switch", f"{LEG}_led")).state == STATE_ON


def test_website_parsing():
    assert latest_on_page(PAGE_HTML, "PG2400P", "1.0") == "1.1.0 Build 20250710"
    assert latest_on_page(PAGE_HTML, "PG2400P", "2.0") == "2.0.0 Build 20260101"
    assert latest_on_page(PAGE_HTML, "PG1200", "1.0") is None
    assert hardware_major("PGW2440 v1.0") == 1
    assert hardware_major("1.0") == 1
    assert hardware_major(None) is None
    assert page_url("PG2405P", "1.0") == (
        "https://www.tp-link.com/fr/support/download/pg2405p-kit/v1/"
    )
    assert page_url("PG1200", "1.0").endswith("/en/support/download/pg1200-kit/v1/")
    assert page_url("../evil", "1.0") is None
    assert page_url("PG2400P", None) is None


def test_versions():
    assert parse_version("1.0.3 Build 20221213 Rel.62540") == (1, 0, 3, 20221213)
    assert short_version("1.0.3 Build 20221213 Rel.62540") == "1.0.3 Build 20221213"
    assert short_version("weird") == "weird"
    newer = FirmwareUpdate.version_is_newer
    assert newer(None, "1.1.0 Build 20250710", "1.0.3 Build 20221213")
    assert newer(None, "1.0.3 Build 20230101", "1.0.3 Build 20221213")
    assert not newer(None, "1.0.3 Build 20221213", "1.0.4 Build 20240830")
    assert newer(None, "2.0 beta", "1.0.3 Build 20221213")
    assert release_notes("1. A\\n2. B\\tC \\'x\\'") == "1. A\n2. B C 'x'"
    assert release_notes("  ") is None


# --- Review fixes ---------------------------------------------------------------------


def encrypted_client(hass) -> EncryptedClient:
    return EncryptedClient(
        async_get_clientsession(hass), fixtures.ENCRYPTED_HOST, fixtures.PASSWORD
    )


async def test_nothing_reaches_the_adapter_while_it_writes_firmware(
    hass, encrypted_device, freezer
):
    encrypted_device.firmware_1_1()
    client = encrypted_client(hass)
    firmware = "1.1.0 Build 20250826 Rel.50806"
    with patch("custom_components.tplink_powerline.api.DOWNLOAD_POLL", 0):
        assert await client.install_firmware(firmware, lambda _p: None) == 240
    sent = len(encrypted_device.requests)
    # A restart or a poll queued meanwhile: refused without a single request
    with pytest.raises(PowerlineUpgradingError):
        await client.reboot()
    with pytest.raises(PowerlineUpgradingError):
        await client.state()
    assert len(encrypted_device.requests) == sent
    freezer.tick(timedelta(seconds=241))
    assert (await client.state()).firmware == firmware


async def test_upgrade_never_started_on_an_unexpected_answer(hass, encrypted_device):
    encrypted_device.firmware_1_1()
    encrypted_device.cloud_data = None
    with pytest.raises(PowerlineError, match="unexpected online upgrade answer"):
        await encrypted_client(hass).install_firmware("1.1.0 Build 20250826", lambda _p: None)
    assert not [w for w in encrypted_device.writes if w[1].get("operation") == "upgrade"]
    assert encrypted_device.logouts == encrypted_device.logins


async def test_download_flags_as_text(hass, encrypted_device):
    encrypted_device.firmware_1_1()
    original = encrypted_device._cloud

    def text_flags(method, url, path, params):
        if path.endswith("detect_upgrade_status"):
            percent = (
                encrypted_device.download.pop(0) if len(encrypted_device.download) > 1 else 100
            )
            data = {"percent": str(percent), "download_fail": "0"}
            return encrypted_device._reply(method, url, {"success": True, "data": data})
        return original(method, url, path, params)

    encrypted_device._cloud = text_flags
    seen: list[int] = []
    with patch("custom_components.tplink_powerline.api.DOWNLOAD_POLL", 0):
        await encrypted_client(hass).install_firmware("1.1.0 Build 20250826", seen.append)
    assert seen == [0, 40, 100]


async def test_firmware_check_runs_the_page_checks_first(hass, encrypted_device):
    encrypted_device.firmware_1_1()
    info = await encrypted_client(hass).firmware_info("1.1.0 Build 20250826")
    assert info.latest == "1.1.1 Build 20260301 Rel.12345"
    assert not info.up_to_date
    assert [path.split("form=")[1] for path, _ in cloud_requests(encrypted_device)] == [
        "check_internet",
        "check_device",
        "check_connection",
        "cloud_upgrade",
    ]


@pytest.mark.parametrize(
    "schedule", [[], [{"shour": "22", "smin": "30", "ehour": "24", "emin": "0"}], {}]
)
async def test_unreadable_schedule_is_never_replaced(hass, encrypted_device, schedule):
    encrypted_device.data["schedule"] = schedule
    with pytest.raises(PowerlineError, match="night mode schedule"):
        await encrypted_client(hass).set_night_mode(True)
    assert not [w for w in encrypted_device.writes if w[0] == LED]


async def test_legacy_session_error_fails_the_poll(hass, serve):
    device = fixtures.LegacyDevice()
    original = device.__call__

    async def expired_at_night_read(method, url, data):
        if "TPLINK.LEDSCHEDULE.ENABLE" in url.query_string:
            return fixtures._response(method, url, "ERROR=003\r\n")
        return await original(method, url, data)

    serve(expired_at_night_read, fixtures.LEGACY_HOST)
    client = LegacyClient(async_get_clientsession(hass), fixtures.LEGACY_HOST, fixtures.PASSWORD)
    with pytest.raises(PowerlineError, match="not authenticated"):
        await client.state()


async def test_no_guest_reads_without_wifi(hass, encrypted_device):
    encrypted_device.missing.add("/admin/wlan_status")
    state = await encrypted_client(hass).state()
    assert (state.guest_2g, state.guest_5g) == (None, None)
    assert not [p for p, _ in encrypted_device.requests if p.startswith("/admin/guest")]


async def test_failed_firmware_check_is_retried_soon(
    hass, encrypted_entry_factory, encrypted_device, advance
):
    encrypted_device.firmware_1_1()
    encrypted_device.fail_on["/admin/cloud_account?form=cloud_upgrade"] = "cloud error"
    await encrypted_entry_factory()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entity_id(hass, "update", f"{ENC}_firmware") is None
    del encrypted_device.fail_on["/admin/cloud_account?form=cloud_upgrade"]
    await advance(timedelta(minutes=16))
    assert hass.states.get(entity_id(hass, "update", f"{ENC}_firmware")).state == STATE_ON


async def test_check_for_updates_reruns_the_firmware_check(
    hass, encrypted_entry_factory, encrypted_device
):
    encrypted_device.firmware_1_1()
    await encrypted_entry_factory()
    await hass.async_block_till_done(wait_background_tasks=True)
    firmware = entity_id(hass, "update", f"{ENC}_firmware")
    encrypted_device.cloud_data |= {"latest_version": "1.2.0 Build 20260601 Rel.1"}
    await async_setup_component(hass, "homeassistant", {})
    await call(hass, "homeassistant", "update_entity", firmware)
    assert hass.states.get(firmware).attributes["latest_version"] == "1.2.0 Build 20260601"


# --- Second review fixes --------------------------------------------------------------


async def test_new_firmware_is_checked_with_the_new_version(
    hass, encrypted_entry_factory, encrypted_device, next_poll
):
    await encrypted_entry_factory()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entity_id(hass, "update", f"{ENC}_firmware") is None
    # Upgraded to 1.1.0 on the web page: the next poll sees it and checks right away
    encrypted_device.firmware_1_1()
    await next_poll()
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(entity_id(hass, "update", f"{ENC}_firmware"))
    assert state.attributes["installed_version"] == "1.1.0 Build 20250826"


async def test_failed_check_does_not_stop_the_firmware_check(hass, encrypted_device):
    encrypted_device.firmware_1_1()
    encrypted_device.fail_on["/admin/cloud_account?form=check_device"] = "not bound"
    info = await encrypted_client(hass).firmware_info("1.1.0 Build 20250826")
    assert info.latest == "1.1.1 Build 20260301 Rel.12345"


async def test_lost_download_still_protects_the_adapter(hass, encrypted_device, freezer):
    encrypted_device.firmware_1_1()
    original = encrypted_device._cloud

    def status_lost(method, url, path, params):
        if path.endswith("detect_upgrade_status"):
            return encrypted_device._reply(method, url, {"success": False, "errorcode": "x"})
        return original(method, url, path, params)

    encrypted_device._cloud = status_lost
    client = encrypted_client(hass)
    with (
        patch("custom_components.tplink_powerline.api.DOWNLOAD_POLL", 0),
        pytest.raises(PowerlineError),
    ):
        await client.install_firmware("1.1.0 Build 20250826", lambda _p: None)
    # The adapter accepted the upgrade and may be writing: left alone all the same
    with pytest.raises(PowerlineUpgradingError):
        await client.state()


async def test_reported_download_failure_leaves_the_adapter_usable(hass, encrypted_device):
    encrypted_device.firmware_1_1()
    encrypted_device.download_fail = True
    client = encrypted_client(hass)
    with (
        patch("custom_components.tplink_powerline.api.DOWNLOAD_POLL", 0),
        pytest.raises(PowerlineError, match="could not download"),
    ):
        await client.install_firmware("1.1.0 Build 20250826", lambda _p: None)
    assert (await client.state()).firmware.startswith("1.1.0")


async def test_no_logout_while_the_adapter_writes_firmware(hass, encrypted_device):
    encrypted_device.firmware_1_1()
    with patch("custom_components.tplink_powerline.api.DOWNLOAD_POLL", 0):
        await encrypted_client(hass).install_firmware("1.1.0 Build 20250826", lambda _p: None)
    assert encrypted_device.logouts == encrypted_device.logins - 1


async def test_no_upgrade_when_already_up_to_date(hass, encrypted_device):
    encrypted_device.firmware_1_1()
    encrypted_device.cloud_data |= {"latest_flag": True}
    with pytest.raises(PowerlineError, match="latest firmware"):
        await encrypted_client(hass).install_firmware("1.1.0 Build 20250826", lambda _p: None)
    assert not [w for w in encrypted_device.writes if w[1].get("operation") == "upgrade"]


@pytest.mark.parametrize("flag", [True, 1, "1", "true"])
async def test_download_failure_codes(hass, encrypted_device, flag):
    encrypted_device.firmware_1_1()
    original = encrypted_device._cloud

    def failing(method, url, path, params):
        if path.endswith("detect_upgrade_status"):
            data = {"percent": "10", "download_fail": flag}
            return encrypted_device._reply(method, url, {"success": True, "data": data})
        return original(method, url, path, params)

    encrypted_device._cloud = failing
    with (
        patch("custom_components.tplink_powerline.api.DOWNLOAD_POLL", 0),
        pytest.raises(PowerlineError, match="could not download"),
    ):
        await encrypted_client(hass).install_firmware("1.1.0 Build 20250826", lambda _p: None)


async def test_legacy_night_read_http_error_hides_only_night_mode(hass, serve):
    device = fixtures.LegacyDevice()
    original = device.__call__

    async def http_error(method, url, data):
        if "TPLINK.LEDSCHEDULE.ENABLE" in url.query_string:
            return AiohttpClientMockResponse(method, url, status=500)
        return await original(method, url, data)

    serve(http_error, fixtures.LEGACY_HOST)
    client = LegacyClient(async_get_clientsession(hass), fixtures.LEGACY_HOST, fixtures.PASSWORD)
    state = await client.state()
    assert state.night_mode is None
    assert state.led is True


async def test_firmware_check_backs_off(hass, encrypted_entry_factory, encrypted_device):
    encrypted_device.firmware_1_1()
    encrypted_device.fail_on["/admin/cloud_account?form=cloud_upgrade"] = "cloud error"
    entry = await encrypted_entry_factory()
    await hass.async_block_till_done(wait_background_tasks=True)
    firmware = entry.runtime_data.firmware
    delays = [firmware._next_retry()]
    for _ in range(7):
        await firmware.async_refresh()
        delays.append(firmware._next_retry())
    assert delays[:4] == [1800, 3600, 7200, 14400]
    assert delays[-1] == 43200


# --- Third review fixes ---------------------------------------------------------------


@pytest.mark.parametrize("flag", ["null", "none", "ok", 2])
async def test_unclear_download_flags_are_not_failures(hass, encrypted_device, flag):
    encrypted_device.firmware_1_1()
    original = encrypted_device._cloud

    def unclear(method, url, path, params):
        if path.endswith("detect_upgrade_status"):
            percent = (
                encrypted_device.download.pop(0) if len(encrypted_device.download) > 1 else 100
            )
            data = {"percent": str(percent), "download_fail": flag}
            return encrypted_device._reply(method, url, {"success": True, "data": data})
        return original(method, url, path, params)

    encrypted_device._cloud = unclear
    with patch("custom_components.tplink_powerline.api.DOWNLOAD_POLL", 0):
        assert await encrypted_client(hass).install_firmware(
            "1.1.0 Build 20250826", lambda _p: None
        )


async def test_lost_upgrade_answer_still_protects_the_adapter(hass, encrypted_device):
    encrypted_device.firmware_1_1()
    original = encrypted_device._cloud

    def lost_answer(method, url, path, params):
        if params.get("operation") == "upgrade":
            encrypted_device.writes.append((path, params))
            return AiohttpClientMockResponse(method, url, exc=aiohttp.ServerDisconnectedError())
        return original(method, url, path, params)

    encrypted_device._cloud = lost_answer
    client = encrypted_client(hass)
    with pytest.raises(PowerlineError):
        await client.install_firmware("1.1.0 Build 20250826", lambda _p: None)
    with pytest.raises(PowerlineUpgradingError):
        await client.state()


async def test_unreadable_progress_gives_up(hass, encrypted_device):
    encrypted_device.firmware_1_1()
    original = encrypted_device._cloud

    def weird(method, url, path, params):
        if path.endswith("detect_upgrade_status"):
            data = {"percent": "50%", "download_fail": "0"}
            return encrypted_device._reply(method, url, {"success": True, "data": data})
        return original(method, url, path, params)

    encrypted_device._cloud = weird
    client = encrypted_client(hass)
    with (
        patch("custom_components.tplink_powerline.api.DOWNLOAD_POLL", 0),
        pytest.raises(PowerlineError, match="unreadable download progress"),
    ):
        await client.install_firmware("1.1.0 Build 20250826", lambda _p: None)
    # Lost track of an accepted upgrade: still left alone
    with pytest.raises(PowerlineUpgradingError):
        await client.state()


async def test_failed_checks_without_an_answer_are_not_up_to_date(hass, encrypted_device):
    encrypted_device.firmware_1_1()
    encrypted_device.fail_on["/admin/cloud_account?form=check_internet"] = "no internet"
    encrypted_device.cloud_data |= {"latest_version": "", "latest_flag": False}
    with pytest.raises(PowerlineError, match="check failed"):
        await encrypted_client(hass).firmware_info("1.1.0 Build 20250826")


async def test_unwritable_switches_show_their_state_and_refuse_both_ways(hass, encrypted_device):
    encrypted_device.data[GUEST_2G] |= {"guest_2g_psk_version": "rsn", "guest_2g_enable": "on"}
    encrypted_device.data["schedule"] = [{"shour": "22", "smin": "30", "ehour": "24", "emin": "0"}]
    client = encrypted_client(hass)
    state = await client.state()
    assert state.guest_2g is True
    assert state.night_mode is False
    with pytest.raises(PowerlineError, match="security version"):
        await client.set_guest("2g", False)
    with pytest.raises(PowerlineError, match="night mode schedule"):
        await client.set_night_mode(False)
    assert not [w for w in encrypted_device.writes if w[0] in (GUEST_2G, LED)]


async def test_busy_web_ui_does_not_back_off(hass, encrypted_entry_factory, encrypted_device):
    encrypted_device.firmware_1_1()
    entry = await encrypted_entry_factory()
    await hass.async_block_till_done(wait_background_tasks=True)
    firmware = entry.runtime_data.firmware
    encrypted_device._logged_in = True  # someone else logged in to the web page
    for _ in range(3):
        await firmware.async_refresh()
    assert firmware._next_retry() == 900
