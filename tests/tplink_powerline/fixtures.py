"""Fake adapters speaking each firmware's protocol, with values recorded on real devices.

Addresses are anonymised; everything else is what a PGW2440 (fw 1.0.4) and a PG2400P
(fw 1.0.3) answered.
"""

from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs

import aiohttp
from cryptography.hazmat.primitives.asymmetric import rsa
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.tplink_powerline.api import EncryptedClient

PASSWORD = "plc-secret"
ENCRYPTED_HOST = "192.0.2.20"
LEGACY_HOST = "192.0.2.10"
ENCRYPTED_MAC = "02:00:00:00:00:b2"
LEGACY_MAC = "02:00:00:00:00:a1"

ENCRYPTED_DATA = {
    "/admin/ghn_powerline?form=plc_settings": {
        "mac": "02-00-00-00-00-b2",
        "device_name": "Garage",
        "network_name": "home@powergrid",
    },
    "/admin/firmware?form=get_firmware_info": {
        "firmware_version": "1.0.4 Build 20240830 Rel.67970",
        "hardware_version": "PGW2440 v1.0",
        "model": "PGW2440",
    },
    "/admin/powerline?form=plc_device": [
        {
            "device_mac": "02-00-00-00-00-a1",
            "device_password": "",
            "rx_rate": "105",
            "tx_rate": "103",
            "status": "on",
        }
    ],
    "/admin/ledSettings?form=enable": {"enable": "1", "module_enable": "on"},
    "/admin/wlan_status": {
        "wireless_2g_enable": "on",
        "wireless_2g_ssid": "Home",
        "wireless_2g_pwd": "wifi-secret",
        "wireless_2g_channel": 1,
        "wireless_5g_enable": "off",
        "wireless_5g_ssid": "Home-5G",
        "wireless_5g_pwd": "wifi-secret",
        "wireless_5g_channel": 36,
    },
    "/admin/wireless?form=statistics": [
        {
            "mac": "02-00-00-00-00-c3",
            "type": "2.4GHz",
            "encryption": "wpa2-psk",
            "rxpkts": "0",
            "txpkts": "0",
            "ip": "192.0.2.50",
            "devType": "none",
            "devName": "laptop",
        }
    ],
    "/admin/ethernet?form=get_ethernet_status": [
        {"type": "lan", "status": "unconnected", "duplex": "HALF", "speed": 0, "serial": 1}
    ],
    "/admin/wireless?form=wireless_2g": {
        "enable": "on",
        "ssid": "Home",
        "hidden": "off",
        "encryption": "psk",
        "psk_version": "rsn",
        "psk_cipher": "aes",
        "psk_key": "wifi-secret",
        "hwmode": "bgn",
        "htmode": "auto",
        "channel": "1",
        "txpower": "high",
        "disabled": "off",
    },
}

LEGACY_DATA = {
    "SYSTEM.GENERAL.FW_VERSION": "1.0.3 Build 20221213 Rel.62540",
    "SYSTEM.PRODUCTION.HW_PRODUCT": "PG2400P",
    "SYSTEM.PRODUCTION.HW_REVISION": "1.0",
    "SYSTEM.PRODUCTION.MAC_ADDR": "02:00:00:00:00:a1",
    "NODE.GENERAL.DEVICE_NAME": "Study",
    "DIDMNG.GENERAL.NUM_DIDS": "2",
    "DIDMNG.GENERAL.MACS": "00:00:00:00:00:00,02:00:00:00:00:b2,02:00:00:00:00:a1",
    "DIDMNG.GENERAL.RX_BPS": "0,3299,0",
    "DIDMNG.GENERAL.TX_BPS": "0,3374,0",
    "TPLINK.LEDSCHEDULE.LED_ENABLE": "YES",
}
# Key groups the legacy web UI reads; the device drops any other grouping
LEGACY_GROUPS = [
    {"SYSTEM.GENERAL.FW_VERSION", "SYSTEM.PRODUCTION.HW_PRODUCT", "SYSTEM.PRODUCTION.HW_REVISION"},
    {"SYSTEM.PRODUCTION.MAC_ADDR", "NODE.GENERAL.DEVICE_NAME"},
    {"DIDMNG.GENERAL.MACS", "DIDMNG.GENERAL.RX_BPS", "DIDMNG.GENERAL.TX_BPS"},
    {"DIDMNG.GENERAL.NUM_DIDS"},
    {"TPLINK.LEDSCHEDULE.LED_ENABLE"},
]

_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=1024)


def _response(method, url, text: str) -> AiohttpClientMockResponse:
    return AiohttpClientMockResponse(method, url, text=text)


def _path(url) -> str:
    return url.path + (f"?{url.query_string}" if url.query_string else "")


class EncryptedDevice:
    """A PGW2440: learns the AES key from the signed login, checks every signature."""

    def __init__(self, password: str = PASSWORD) -> None:
        numbers = _RSA_KEY.private_numbers()
        self._n, self._d = numbers.public_numbers.n, numbers.d
        self.modulus = format(self._n, "X")
        self.seq = 1000
        self.password = password
        self.data = json.loads(json.dumps(ENCRYPTED_DATA))
        self.writes: list[tuple[str, dict]] = []
        self.logins = self.logouts = self.failures = 0
        self.fail_with: str | None = None
        # Session opened, but the answer to the login never arrives
        self.lose_login_answer = False
        # Endpoints this model lacks
        self.missing: set[str] = set()
        # Endpoint -> error code answered there, e.g. a session that expired
        self.fail_on: dict[str, str] = {}
        # Restarts before answering
        self.drop_reboot_answer = False
        self.restarting = False
        # Forced answer to the next logins, e.g. "exceeded max attempts"
        self.login_error: str | None = None
        self._aes = EncryptedClient(None, ENCRYPTED_HOST, "")
        self._logged_in = False

    def rsa_decrypt(self, hex_text: str) -> str:
        size = len(self.modulus)
        out = ""
        for i in range(0, len(hex_text), size):
            value = pow(int(hex_text[i : i + size], 16), self._d, self._n)
            out += value.to_bytes((self._n.bit_length() + 7) // 8, "big").rstrip(b"\0").decode()
        return out

    def _reply(self, method, url, payload: dict) -> AiohttpClientMockResponse:
        return _response(method, url, json.dumps({"data": self._aes.encrypt(json.dumps(payload))}))

    async def __call__(self, method, url, data):
        if self.restarting:
            return AiohttpClientMockResponse(method, url, exc=aiohttp.ServerDisconnectedError())
        path = _path(url)
        form = {k: v[0] for k, v in parse_qs(data or "", keep_blank_values=True).items()}
        if path == "/admin/locale?form=index_lang":
            return _response(
                method, url, json.dumps({"success": True, "data": {"model": "PGW2440"}})
            )
        if path == "/login?form=keys":
            body = {"password": [self.modulus, "010001"], "mode": "rsa"}
            return _response(method, url, json.dumps({"success": True, "data": body}))
        if path == "/login?form=auth":
            body = {"key": [self.modulus, "010001"], "seq": str(self.seq)}
            return _response(method, url, json.dumps({"success": True, "data": body}))
        if path == "/admin/firmware?form=get_firmware_info":
            body = {"success": True, "data": self.data[path]}
            return _response(method, url, json.dumps(body))

        signed = dict(part.split("=", 1) for part in self.rsa_decrypt(form["sign"]).split("&"))
        expected_hash = hashlib.md5(self.password.encode()).hexdigest()
        assert int(signed["s"]) == self.seq + len(form["data"])
        if path == "/login?form=login":
            if self._logged_in:
                # One admin session at a time, as on the real adapter
                return self._reply(method, url, {"success": False, "errorcode": "user conflict"})
            self._aes._key, self._aes._iv = signed["k"], signed["i"]
            if self.login_error:
                return self._reply(method, url, {"success": False, "errorcode": self.login_error})
            params = dict(parse_qs(self._aes.decrypt(form["data"])))
            assert params["operation"] == ["login"]
            password = self.rsa_decrypt(params["password"][0])
            if password != self.password or signed["h"] != expected_hash:
                self.failures += 1
                return self._reply(
                    method,
                    url,
                    {
                        "success": False,
                        "errorcode": "login failed",
                        "data": {
                            "failureCount": self.failures,
                            "attemptsAllowed": 10 - self.failures,
                        },
                    },
                )
            self.logins += 1
            self._logged_in = True
            if self.lose_login_answer:
                return AiohttpClientMockResponse(method, url, exc=aiohttp.ServerDisconnectedError())
            return self._reply(method, url, {"success": True})

        if path == "/logout" and not self._logged_in:
            # A logout tried after a failed login: refused, like the real adapter does
            return _response(method, url, json.dumps({"success": False, "errorcode": "-1"}))
        assert self._logged_in, f"{path} before login"
        assert signed["h"] == expected_hash
        params = {k: v[0] for k, v in parse_qs(self._aes.decrypt(form["data"])).items()}
        if path == "/logout":
            self.logouts += 1
            self._logged_in = False
            return self._reply(method, url, {"success": True})
        if self.fail_with:
            return self._reply(method, url, {"success": False, "errorcode": self.fail_with})
        if path in self.missing:
            return self._reply(method, url, {"success": False, "errCode": -1})
        if path in self.fail_on:
            return self._reply(method, url, {"success": False, "errorcode": self.fail_on[path]})
        operation = params.pop("operation")
        if operation == "write":
            self.writes.append((path, params))
            if path == "/admin/reboot.json" and self.drop_reboot_answer:
                self.restarting = True
                return AiohttpClientMockResponse(method, url, exc=aiohttp.ServerDisconnectedError())
            if path == "/admin/ledSettings?form=enable":
                self.data[path]["enable"] = "1" if params["toggle"] == "on" else "0"
            return self._reply(method, url, {"success": True, "data": self.data.get(path, {})})
        lists = ("/admin/powerline?form=plc_device", "/admin/wireless?form=statistics")
        if path in lists and operation != "load":
            # Lists only answer to `load`
            return self._reply(method, url, {"success": False, "errCode": -1})
        return self._reply(method, url, {"success": True, "data": self.data[path]})


class LegacyDevice:
    """A PG2400P: token in `_t`, reads only in the web UI's key groups."""

    def __init__(self, password: str = PASSWORD) -> None:
        self.password = password
        self.data = dict(LEGACY_DATA)
        self.writes: list[dict] = []
        self.logins = self.logouts = 0
        self.token = ""
        # Forced error code for the next logins, e.g. 7 (locked)
        self.login_code: int | None = None

    async def __call__(self, method, url, data):
        query = parse_qs(url.query_string, keep_blank_values=True)
        form = {k: v[0] for k, v in parse_qs(data or "", keep_blank_values=True).items()}
        token = query.pop("_t", [""])[0]
        if method == "POST" and not token:
            if form.get("COMMAND") == "is factorydefault":
                return _response(method, url, "ERROR=000\r\nRESULT=0\r\n")
            if self.token:
                # One admin session at a time, as on the real adapter
                return _response(method, url, "ERROR=004\r\n")
            if self.login_code is not None:
                return _response(method, url, f"ERROR={self.login_code:03d}\r\n")
            digest = hashlib.md5(self.password.encode()).hexdigest()
            if form.get("TPLINK.GENERAL.LOGIN_PASSWORD") != digest:
                return _response(method, url, "ERROR=006\r\nLOGIN_TIMES=1\r\n")
            self.logins += 1
            self.token = "tok123"
            return _response(method, url, f"ERROR=000\r\nTOKEN={self.token}\r\n")
        if not self.token or token != self.token:
            return _response(method, url, "ERROR=003\r\n")
        if method == "GET":
            keys = set(query)
            assert keys in LEGACY_GROUPS, f"unknown key group {keys}"
            lines = "".join(f"{key}={self.data[key]}\r\n" for key in query)
            return _response(method, url, "ERROR=000\r\n" + lines)
        if form.get("COMMAND") == "logout":
            self.logouts += 1
            self.token = ""
            return _response(method, url, "ERROR=000\r\nRESULT=0\r\n")
        self.writes.append(form)
        self.data.update({k: v for k, v in form.items() if k in self.data})
        return _response(method, url, "ERROR=000\r\n")
