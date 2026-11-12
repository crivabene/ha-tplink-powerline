"""Clients against fake adapters that check the protocol like the real ones."""

import asyncio
import logging
import re
from unittest.mock import patch

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest

from custom_components.tplink_powerline.api import (
    FAMILY_ENCRYPTED,
    FAMILY_LEGACY,
    EncryptedClient,
    LegacyClient,
    Peer,
    PowerlineAuthError,
    PowerlineBusyError,
    PowerlineConnectionError,
    PowerlineError,
    PowerlineHTTPError,
    PowerlineLockedError,
    PowerlineRejected,
    detect,
    legacy_peers,
    normalize_host,
    rsa_nopad,
    wifi_settings,
)

from . import fixtures


def test_rsa_nopad_matches_raw_rsa():
    device = fixtures.EncryptedDevice()
    encrypted = rsa_nopad("h=abc&s=12", device.modulus, "010001")
    assert len(encrypted) == len(device.modulus)
    assert device.rsa_decrypt(encrypted) == "h=abc&s=12"


def test_aes_round_trip_and_long_signature():
    client = EncryptedClient(None, "192.0.2.20", "pw")
    client._key, client._iv = "1234567890123456", "6543210987654321"
    assert client.decrypt(client.encrypt("operation=read&x=1")) == "operation=read&x=1"
    device = fixtures.EncryptedDevice()
    client._rsa, client._seq = (device.modulus, "010001"), 7
    signature = client.signature(10, login=True)
    # k=…&i=…&h=<32>&s=… is longer than one 64-char block
    assert len(signature) == 2 * len(device.modulus)
    assert device.rsa_decrypt(signature).startswith("k=1234567890123456&i=6543210987654321&h=")


def test_legacy_peers_skip_empty_slot_and_self():
    peers = legacy_peers(
        {
            "DIDMNG.GENERAL.MACS": fixtures.LEGACY_DATA["DIDMNG.GENERAL.MACS"],
            "DIDMNG.GENERAL.RX_BPS": "0,3299,0",
            "DIDMNG.GENERAL.TX_BPS": "0,3374,0",
        },
        fixtures.LEGACY_MAC,
    )
    assert peers == [Peer(fixtures.ENCRYPTED_MAC, 103, 105)]
    assert legacy_peers({"DIDMNG.GENERAL.MACS": "x,y"}, "z") == []


def test_wifi_settings_rewrite_only_enable():
    settings = wifi_settings(fixtures.ENCRYPTED_DATA["/admin/wireless?form=wireless_2g"], False)
    assert settings == {
        "enable": "off",
        "ssid": "Home",
        "hidden": "off",
        "encryption": "psk",
        "hwmode": "bgn",
        "htmode": "auto",
        "channel": "1",
        "txpower": "high",
        "psk_version": "rsn",
        "psk_cipher": "aes",
        "psk_key": "wifi-secret",
    }


async def test_encrypted_state(hass, encrypted_device):
    client = EncryptedClient(
        async_get_clientsession(hass), fixtures.ENCRYPTED_HOST, fixtures.PASSWORD
    )
    state = await client.state()
    assert state.mac == fixtures.ENCRYPTED_MAC
    assert (state.model, state.name, state.hardware) == ("PGW2440", "Garage", "PGW2440 v1.0")
    assert state.firmware == "1.0.4 Build 20240830 Rel.67970"
    assert state.peers == [Peer(fixtures.LEGACY_MAC, 105, 103)]
    assert (state.led, state.wifi_2g, state.wifi_5g) == (True, True, False)
    assert state.wifi_clients == [
        {"mac": "02:00:00:00:00:c3", "ip": "192.0.2.50", "name": "laptop", "band": "2.4GHz"}
    ]
    assert state.ethernet == [{"connected": False, "speed": 0, "duplex": "HALF"}]
    assert (encrypted_device.logins, encrypted_device.logouts) == (1, 1)


async def test_encrypted_commands(hass, encrypted_device):
    client = EncryptedClient(
        async_get_clientsession(hass), fixtures.ENCRYPTED_HOST, fixtures.PASSWORD
    )
    await client.set_led(False)
    await client.set_wifi("2g", False)
    await client.reboot()
    assert encrypted_device.writes[0] == (
        "/admin/ledSettings?form=enable",
        {"enable": "toggle", "toggle": "off"},
    )
    path, written = encrypted_device.writes[1]
    assert path == "/admin/wireless?form=wireless_2g"
    assert written["enable"] == "off" and written["ssid"] == "Home"
    assert encrypted_device.writes[2] == ("/admin/reboot.json", {})
    assert encrypted_device.logins == encrypted_device.logouts == 3


async def test_encrypted_wrong_password(hass, encrypted_device):
    client = EncryptedClient(async_get_clientsession(hass), fixtures.ENCRYPTED_HOST, "wrong")
    with pytest.raises(PowerlineAuthError, match="login failed"):
        await client.state()
    assert encrypted_device.failures == 1


async def test_encrypted_error_still_logs_out(hass, encrypted_device):
    encrypted_device.fail_with = "timeout"
    client = EncryptedClient(
        async_get_clientsession(hass), fixtures.ENCRYPTED_HOST, fixtures.PASSWORD
    )
    with pytest.raises(PowerlineError, match="timeout"):
        await client.state()
    assert encrypted_device.logouts == 1


async def test_legacy_state_and_commands(hass, legacy_device):
    client = LegacyClient(async_get_clientsession(hass), fixtures.LEGACY_HOST, fixtures.PASSWORD)
    state = await client.state()
    assert state.mac == fixtures.LEGACY_MAC
    assert (state.model, state.name, state.hardware) == ("PG2400P", "Study", "1.0")
    assert state.peers == [Peer(fixtures.ENCRYPTED_MAC, 103, 105)]
    assert state.led is True
    assert (state.wifi_2g, state.wifi_clients, state.ethernet) == (None, None, None)
    await client.set_led(False)
    await client.reboot()
    assert legacy_device.writes == [
        {"TPLINK.LEDSCHEDULE.LED_ENABLE": "NO"},
        {"SYSTEM.GENERAL.HW_RESET": "1"},
    ]
    assert legacy_device.logins == legacy_device.logouts == 3
    with pytest.raises(PowerlineError, match="no Wi-Fi"):
        await client.set_wifi("2g", True)


async def test_legacy_wrong_password(hass, legacy_device):
    client = LegacyClient(async_get_clientsession(hass), fixtures.LEGACY_HOST, "wrong")
    with pytest.raises(PowerlineAuthError, match="wrong password"):
        await client.state()


async def test_detect(hass, encrypted_device, legacy_device):
    session = async_get_clientsession(hass)
    assert (await detect(session, fixtures.ENCRYPTED_HOST, "x")).family == FAMILY_ENCRYPTED
    assert (await detect(session, fixtures.LEGACY_HOST, "x")).family == FAMILY_LEGACY


async def test_detect_unsupported(hass, aioclient_mock):
    aioclient_mock.post("http://192.0.2.9/admin/locale?form=index_lang", text="<html>")
    aioclient_mock.post("http://192.0.2.9/", text="<html>")
    with pytest.raises(PowerlineError, match="not a supported"):
        await detect(async_get_clientsession(hass), "192.0.2.9", "x")


async def test_lost_login_answer_still_logs_out(hass, encrypted_device):
    encrypted_device.lose_login_answer = True
    client = EncryptedClient(
        async_get_clientsession(hass), fixtures.ENCRYPTED_HOST, fixtures.PASSWORD
    )
    with pytest.raises(PowerlineConnectionError):
        await client.state()
    # The session the adapter opened is closed, so the web page stays usable
    assert (encrypted_device.logins, encrypted_device.logouts) == (1, 1)


async def test_models_without_wifi_endpoints(hass, encrypted_device):
    encrypted_device.missing = {
        "/admin/wlan_status",
        "/admin/wireless?form=statistics",
        "/admin/ethernet?form=get_ethernet_status",
    }
    client = EncryptedClient(
        async_get_clientsession(hass), fixtures.ENCRYPTED_HOST, fixtures.PASSWORD
    )
    state = await client.state()
    assert (state.wifi_2g, state.wifi_5g, state.wifi_clients, state.ethernet) == (
        None,
        None,
        None,
        None,
    )
    assert state.peers


async def test_unexpected_data_is_a_powerline_error(hass, encrypted_device):
    del encrypted_device.data["/admin/ghn_powerline?form=plc_settings"]["mac"]
    client = EncryptedClient(
        async_get_clientsession(hass), fixtures.ENCRYPTED_HOST, fixtures.PASSWORD
    )
    with pytest.raises(PowerlineError, match=r"unexpected data \(KeyError\)"):
        await client.state()
    assert encrypted_device.logouts == 1


async def test_restart_without_answer_is_a_restart(hass, encrypted_device):
    encrypted_device.drop_reboot_answer = True
    client = EncryptedClient(
        async_get_clientsession(hass), fixtures.ENCRYPTED_HOST, fixtures.PASSWORD
    )
    await client.reboot()
    assert encrypted_device.writes == [("/admin/reboot.json", {})]


def test_wifi_settings_wpa3_and_unknown_security():
    wpa3 = {"enable": "on", "ssid": "Home", "encryption": "psk", "psk_version": "sae"}
    wpa3 |= {"psk_cipher": "aes", "psk_key": "k"}
    assert "psk_cipher" not in wifi_settings(wpa3, True)
    assert wifi_settings(wpa3, True)["psk_key"] == "k"
    assert wifi_settings({"enable": "on", "encryption": "none"}, False)["enable"] == "off"
    with pytest.raises(PowerlineError, match="unsupported Wi-Fi security"):
        wifi_settings({"enable": "on", "encryption": "ent"}, False)


def test_normalize_host():
    for raw in ("192.0.2.20", " http://192.0.2.20/ ", "https://192.0.2.20"):
        assert normalize_host(raw) == "192.0.2.20"


async def test_legacy_requests_are_paced(hass, legacy_device):
    client = LegacyClient(async_get_clientsession(hass), fixtures.LEGACY_HOST, fixtures.PASSWORD)
    with (
        patch("custom_components.tplink_powerline.api.LEGACY_PACING", 0.5),
        patch("custom_components.tplink_powerline.api.time.monotonic", return_value=100.0),
        patch("custom_components.tplink_powerline.api.asyncio.sleep") as sleep,
    ):
        await client.state()
    # login, 5 reads, logout: every request after the first waits, login included
    assert [call.args[0] for call in sleep.call_args_list] == [0.5] * 6


async def test_errors_never_carry_the_session_token(hass, aioclient_mock, legacy_device):
    client = LegacyClient(async_get_clientsession(hass), fixtures.LEGACY_HOST, fixtures.PASSWORD)
    await client._login()
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"http://{fixtures.LEGACY_HOST}/", exc=aiohttp.TooManyRedirects(None, ()))
    with pytest.raises(PowerlineConnectionError) as err:
        await client.read("TPLINK.LEDSCHEDULE.LED_ENABLE")
    assert legacy_device.token not in str(err.value)
    assert "_t=" not in str(err.value)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("user conflict", PowerlineBusyError),
        ("exceeded max attempts", PowerlineLockedError),
        # Unknown refusals count as a rejected password: never retried on their own
        ("something new", PowerlineAuthError),
    ],
)
async def test_encrypted_login_refusals(hass, encrypted_device, error, expected):
    encrypted_device.login_error = error
    client = EncryptedClient(
        async_get_clientsession(hass), fixtures.ENCRYPTED_HOST, fixtures.PASSWORD
    )
    with pytest.raises(expected) as err:
        await client.state()
    assert type(err.value) is expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (4, PowerlineBusyError),
        (8, PowerlineBusyError),
        (7, PowerlineLockedError),
        (6, PowerlineAuthError),
        (2, PowerlineAuthError),
        (10, PowerlineError),
    ],
)
async def test_legacy_login_refusals(hass, legacy_device, code, expected):
    legacy_device.login_code = code
    client = LegacyClient(async_get_clientsession(hass), fixtures.LEGACY_HOST, fixtures.PASSWORD)
    with pytest.raises(expected) as err:
        await client.state()
    assert type(err.value) is expected


async def test_requests_look_like_the_web_page(
    hass, aioclient_mock, encrypted_device, legacy_device
):
    session = async_get_clientsession(hass)
    await EncryptedClient(session, fixtures.ENCRYPTED_HOST, fixtures.PASSWORD).state()
    await LegacyClient(session, fixtures.LEGACY_HOST, fixtures.PASSWORD).state()
    assert aioclient_mock.mock_calls
    for _method, url, _data, headers in aioclient_mock.mock_calls:
        origin = f"http://{url.host}"
        assert headers["Origin"] == origin
        assert headers["Referer"] == f"{origin}/"
        assert headers["X-Requested-With"] == "XMLHttpRequest"
        assert headers["Content-Type"] == "application/x-www-form-urlencoded"
        assert headers["User-Agent"].startswith("Mozilla/")


def test_wifi_settings_wep():
    current = {"enable": "on", "ssid": "Old", "encryption": "wep", "wep_mode": "auto"}
    current |= {"wep_select": "3", "wep_type1": "64", "wep_format1": "hex", "wep_key1": "k"}
    current |= {"wep_key2": "other"}
    settings = wifi_settings(current, False)
    assert settings["wep_select"] == "1"
    assert settings["wep_key1"] == "k"
    assert "wep_key2" not in settings


async def test_wifi_key_never_logged(hass, encrypted_device, caplog):
    caplog.set_level(logging.DEBUG)
    client = EncryptedClient(
        async_get_clientsession(hass), fixtures.ENCRYPTED_HOST, fixtures.PASSWORD
    )
    encrypted_device.data["/admin/wireless?form=wireless_2g"]["encryption"] = "ent"
    with pytest.raises(PowerlineError) as err:
        await client.set_wifi("2g", False)
    assert "wifi-secret" not in str(err.value)
    assert "wifi-secret" not in caplog.text


async def test_clients_of_one_adapter_share_its_session(hass, legacy_device):
    session = async_get_clientsession(hass)
    first = LegacyClient(session, fixtures.LEGACY_HOST, fixtures.PASSWORD)
    second = LegacyClient(session, f"http://{fixtures.LEGACY_HOST}/", fixtures.PASSWORD)
    # The fake adapter refuses a second session: they must take turns
    await asyncio.gather(first.state(), second.state(), first.set_led(False))
    assert legacy_device.logins == legacy_device.logouts == 3


async def test_detect_paces_the_legacy_probe(hass, legacy_device, aioclient_mock):
    aioclient_mock.post(f"http://{fixtures.LEGACY_HOST}/admin/locale?form=index_lang", status=404)
    with (
        patch("custom_components.tplink_powerline.api.LEGACY_PACING", 0.5),
        patch("custom_components.tplink_powerline.api.time.monotonic", return_value=50.0),
        patch("custom_components.tplink_powerline.api.asyncio.sleep") as sleep,
    ):
        client = await detect(async_get_clientsession(hass), fixtures.LEGACY_HOST, "x")
    assert client.family == FAMILY_LEGACY
    assert [call.args[0] for call in sleep.call_args_list] == [0.5]


async def test_detect_waits_for_the_adapter(hass, encrypted_device):
    session = async_get_clientsession(hass)
    holder = EncryptedClient(session, fixtures.ENCRYPTED_HOST, fixtures.PASSWORD)
    await holder.lock.acquire()
    try:
        probe = hass.async_create_task(detect(session, fixtures.ENCRYPTED_HOST, "x"))
        for _ in range(20):
            await asyncio.sleep(0)
        assert not probe.done()
    finally:
        holder.lock.release()
    assert (await probe).family == FAMILY_ENCRYPTED


async def test_logins_left_unanswered_stop_like_a_rejection(hass, encrypted_device):
    encrypted_device.lose_login_answer = True
    client = EncryptedClient(
        async_get_clientsession(hass), fixtures.ENCRYPTED_HOST, fixtures.PASSWORD
    )
    for _ in range(2):
        with pytest.raises(PowerlineConnectionError):
            await client.state()
    with pytest.raises(PowerlineAuthError, match="left unanswered"):
        await client.state()
    logins = encrypted_device.logins
    with pytest.raises(PowerlineAuthError, match="rejected earlier"):
        await client.state()
    assert encrypted_device.logins == logins


def test_normalize_host_case():
    assert normalize_host("HTTP://Adapter.Local/") == "adapter.local"


async def test_optional_endpoint_missing_with_404(hass, encrypted_device, aioclient_mock):
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"http://{fixtures.ENCRYPTED_HOST}/admin/wlan_status", status=404)
    aioclient_mock.post(
        re.compile(rf"http://{re.escape(fixtures.ENCRYPTED_HOST)}/.*"), side_effect=encrypted_device
    )
    client = EncryptedClient(
        async_get_clientsession(hass), fixtures.ENCRYPTED_HOST, fixtures.PASSWORD
    )
    state = await client.state()
    assert (state.wifi_2g, state.wifi_5g) == (None, None)
    assert state.wifi_clients is not None


async def test_http_errors_are_not_lost_logins_or_restarts(hass, encrypted_device, aioclient_mock):
    aioclient_mock.clear_requests()
    host = fixtures.ENCRYPTED_HOST
    aioclient_mock.post(f"http://{host}/login?form=login", status=500)
    aioclient_mock.post(re.compile(rf"http://{re.escape(host)}/.*"), side_effect=encrypted_device)
    client = EncryptedClient(async_get_clientsession(hass), host, fixtures.PASSWORD)
    for _ in range(4):
        # An answer, even an error, is not a lost login: never turned into a rejection
        with pytest.raises(PowerlineHTTPError):
            await client.state()
    # The adapter may have opened a session before failing: a logout was tried each time
    logouts = [url for _m, url, _d, _h in aioclient_mock.mock_calls if url.path == "/logout"]
    assert len(logouts) == 4


async def test_restart_refused_with_http_error(hass, encrypted_device, aioclient_mock):
    aioclient_mock.clear_requests()
    host = fixtures.ENCRYPTED_HOST
    aioclient_mock.post(f"http://{host}/admin/reboot.json", status=403)
    aioclient_mock.post(re.compile(rf"http://{re.escape(host)}/.*"), side_effect=encrypted_device)
    client = EncryptedClient(async_get_clientsession(hass), host, fixtures.PASSWORD)
    with pytest.raises(PowerlineHTTPError):
        await client.reboot()


async def test_session_timeout_fails_the_poll(hass, encrypted_device, aioclient_mock):
    encrypted_device.fail_on = {"/admin/wlan_status": "timeout"}
    client = EncryptedClient(
        async_get_clientsession(hass), fixtures.ENCRYPTED_HOST, fixtures.PASSWORD
    )
    with pytest.raises(PowerlineRejected, match="timeout"):
        await client.state()
