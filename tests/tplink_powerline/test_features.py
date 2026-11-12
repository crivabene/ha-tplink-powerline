"""LED night mode, guest networks and firmware updates (adapter's own and website check)."""

from unittest.mock import patch

from homeassistant.components.update import ATTR_IN_PROGRESS, UpdateEntityFeature
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest

from custom_components.tplink_powerline.api import (
    EncryptedClient,
    LegacyClient,
    PowerlineError,
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
    current = fixtures.ENCRYPTED_DATA[GUEST_2G] | {"guest_2g_psk_version": "sae_transition"}
    # WPA3 is "psk" with an SAE version: written back as the page does
    assert guest_settings(current, "2g", True)["psk_version"] == "saeAuto"
    assert guest_settings(current, "2g", True)["encryption"] == "psk"
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


async def test_online_upgrade(hass, encrypted_entry_factory, encrypted_device):
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

    with (
        patch("custom_components.tplink_powerline.api.DOWNLOAD_POLL", 0),
        patch("custom_components.tplink_powerline.update.wait_for_adapter", adapter_upgrades),
    ):
        await call(hass, "update", "install", firmware)
    # upgrade_time + reboot_time as the adapter announced them
    assert waited == [(180, True, None)]
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
    assert encrypted_device.logouts == encrypted_device.logins
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
