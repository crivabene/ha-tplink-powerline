"""Local clients for TP-Link G.hn powerline adapters, speaking their web UI's API.

Two firmware families exist:

- "encrypted" (e.g. PGW2440): the TP-Link router-style web UI. Requests are form posts,
  AES-CBC encrypted and signed with a raw RSA key the adapter hands out at login.
- "legacy" (e.g. PG2400P): a key/value API over the G.hn data model
  (`DIDMNG.GENERAL.MACS`, ...), with a session token in the `_t` query parameter.

Both accept a single admin session at a time and lock their web UI after about ten wrong
passwords. So every exchange holds the adapter's lock, logs in, does its work and logs
out, and any login refusal that is not clearly "someone else is logged in" counts as a
rejected password: retrying would only bring the lockout closer.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import hashlib
import json
import logging
import secrets
import string
import time
from typing import Any
from urllib.parse import urlencode
import weakref

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=15)
FAMILY_ENCRYPTED = "encrypted"
FAMILY_LEGACY = "legacy"
# The adapters' web servers reset requests that do not look like the browser's
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded",
}
# Legacy firmware drops connections when requests come back to back
LEGACY_PACING = 0.5  # seconds
# A login whose answer never arrives may still have counted as a wrong password: after this
# many in a row (with the adapter answering everything else), stop as if it was rejected
MAX_LOST_LOGINS = 3
# Legacy firmware reports link rates in 1/32 Mbit/s
LEGACY_RATE_DIVISOR = 32
NO_PEER = "00:00:00:00:00:00"


class PowerlineError(Exception):
    """The adapter could not be reached or answered something unexpected."""


class PowerlineConnectionError(PowerlineError):
    """No usable HTTP answer: the adapter is unreachable, restarting or dropped the request."""


class PowerlineHTTPError(PowerlineError):
    """The adapter answered, with an HTTP error status: not a lost request, not a restart."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class PowerlineBusyError(PowerlineError):
    """Someone else is logged in to the web UI: try again later."""


class PowerlineAuthError(PowerlineError):
    """The adapter rejected the password: retrying will not help and may lock it."""


class PowerlineLockedError(PowerlineAuthError):
    """The web UI is locked after too many wrong passwords."""


class PowerlineRejected(PowerlineError):
    """The adapter answered `success: false` (encrypted family)."""

    def __init__(self, path: str, code: str, data: Any = None) -> None:
        super().__init__(f"{path}: {code}")
        self.code = code
        self.data = data


@dataclass(frozen=True)
class Peer:
    """Another adapter on the powerline network, as this one sees it."""

    mac: str
    rx_rate: int  # Mbit/s
    tx_rate: int  # Mbit/s


@dataclass
class PowerlineState:
    """Everything read from one adapter. Wi-Fi fields stay None when the adapter has none,
    or when its answer for them could not be read this time."""

    mac: str
    model: str
    hardware: str | None
    firmware: str | None
    name: str | None
    peers: list[Peer]
    led: bool | None
    wifi_2g: bool | None = None
    wifi_5g: bool | None = None
    wifi_clients: list[dict[str, str]] | None = None
    ethernet: list[dict[str, Any]] | None = None


@dataclass
class _Adapter:
    """Shared by every client of one adapter: one session and one request at a time."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_request: float = float("-inf")
    # Digests of passwords this adapter rejected: an exchange queued behind the one that
    # was refused must not try the same password again
    rejected: set[str] = field(default_factory=set)
    lost_logins: int = 0


# Per event loop, then per adapter address
_ADAPTERS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, _Adapter]] = (
    weakref.WeakKeyDictionary()
)


def _adapter(base: str) -> _Adapter:
    return _ADAPTERS.setdefault(asyncio.get_running_loop(), {}).setdefault(base, _Adapter())


def normalize_host(host: str) -> str:
    """Bare host as stored in the config entry: no scheme, no trailing slash."""
    host = host.strip().lower()
    for scheme in ("http://", "https://"):
        host = host.removeprefix(scheme)
    return host.rstrip("/")


def normalize_mac(mac: str) -> str:
    return mac.strip().lower().replace("-", ":")


def rsa_nopad(text: str, modulus: str, exponent: str) -> str:
    """Raw RSA of the text, zero padded on the right, as the web UI's `nopadding` mode."""
    n, e = int(modulus, 16), int(exponent, 16)
    size = (n.bit_length() + 7) >> 3
    message = int.from_bytes(text.encode().ljust(size, b"\0"), "big")
    return format(pow(message, e, n), "x").rjust(len(modulus), "0")


def _endpoint(path: str) -> str:
    """Path without the query, for messages (the legacy query carries the session token)."""
    return path.split("?", maxsplit=1)[0]


def _digits(count: int) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(count))


def _json(text: str, quote: bool = True) -> dict[str, Any]:
    """Parse an answer; `quote=False` for decrypted text, which may hold a Wi-Fi key."""
    detail = f": {text[:80]!r}" if quote else ""
    try:
        data = json.loads(text)
    except ValueError as err:
        raise PowerlineError(f"unexpected answer{detail}") from err
    if not isinstance(data, dict):
        raise PowerlineError(f"unexpected answer{detail}")
    return data


class PowerlineClient:
    """Common plumbing: one exchange at a time per adapter, login and logout around it."""

    family: str

    def __init__(self, session: aiohttp.ClientSession, host: str, password: str) -> None:
        self._session = session
        self._base = f"http://{normalize_host(host)}"
        self._password = password
        # Set while a config flow tests new settings: a poll waiting for the adapter must
        # not log in with the old password once the flow is done
        self.paused = False

    @property
    def lock(self) -> asyncio.Lock:
        """Held for a whole exchange by every client of this adapter."""
        return _adapter(self._base).lock

    @property
    def headers(self) -> dict[str, str]:
        return BROWSER_HEADERS | {"Referer": f"{self._base}/", "Origin": self._base}

    def _pacing(self) -> float:
        return 0.0

    async def _post(self, path: str, body: str, **kwargs: Any) -> str:
        return await self._send("POST", path, data=body, **kwargs)

    async def _send(self, method: str, path: str, **kwargs: Any) -> str:
        adapter = _adapter(self._base)
        wait = adapter.last_request + self._pacing() - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with self._session.request(
                method, self._base + path, headers=self.headers, timeout=TIMEOUT, **kwargs
            ) as response:
                if response.status != 200:
                    raise PowerlineHTTPError(
                        f"{method} {_endpoint(path)}: HTTP {response.status}", response.status
                    )
                return await response.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            # Only the type: some aiohttp errors quote the URL, and the legacy one holds the token
            raise PowerlineConnectionError(
                f"{method} {_endpoint(path)}: {type(err).__name__}"
            ) from err
        except UnicodeDecodeError as err:
            raise PowerlineError(f"{method} {_endpoint(path)}: undecodable answer") from err
        finally:
            adapter.last_request = time.monotonic()

    async def _login(self) -> None:
        raise NotImplementedError

    async def _logout(self) -> None:
        raise NotImplementedError

    async def _read(self) -> PowerlineState:
        raise NotImplementedError

    async def _safe_logout(self) -> None:
        try:
            await self._logout()
        except PowerlineError as err:
            # The session times out by itself on the adapter
            _LOGGER.debug("Logout from %s failed: %s", self._base, err)

    def _digest(self) -> str:
        return hashlib.sha256(self._password.encode()).hexdigest()

    async def _exchange(self, work, deliberate: bool = False):
        """Login, `work`, logout, holding the adapter. `deliberate`: a login the user asked
        for (config flow), tried even with a password rejected before."""
        adapter = _adapter(self._base)
        async with adapter.lock:
            if self.paused:
                raise PowerlineError("paused while the adapter is being reconfigured")
            if not deliberate and self._digest() in adapter.rejected:
                raise PowerlineAuthError("password rejected earlier")
            try:
                await self._login()
            except PowerlineAuthError:
                adapter.rejected.add(self._digest())
                raise  # no session was opened
            except PowerlineBusyError:
                raise  # no session was opened
            except BaseException:
                # The adapter may have opened the session before the answer got lost, or
                # Home Assistant may be stopping: try to leave the web UI usable
                await self._safe_logout()
                raise
            adapter.rejected.discard(self._digest())
            adapter.lost_logins = 0
            try:
                return await work()
            except (AttributeError, IndexError, KeyError, TypeError, ValueError) as err:
                # Only the type: the data may hold a Wi-Fi key
                raise PowerlineError(f"unexpected data ({type(err).__name__})") from err
            finally:
                await self._safe_logout()

    async def state(self) -> PowerlineState:
        return await self._exchange(self._read)

    async def check(self) -> PowerlineState:
        """Log in once and read, as the user asked: used to validate a password."""
        return await self._exchange(self._read, deliberate=True)

    async def set_led(self, on: bool) -> None:
        raise NotImplementedError

    async def reboot(self) -> None:
        raise NotImplementedError

    async def set_wifi(self, band: str, on: bool) -> None:
        raise PowerlineError("this adapter has no Wi-Fi")


class EncryptedClient(PowerlineClient):
    """PGW2440-style firmware: AES-CBC data signed with the adapter's RSA key."""

    family = FAMILY_ENCRYPTED
    # Endpoints the web UI calls in clear text
    PLAIN = ("/login?form=keys", "/login?form=auth", "/admin/firmware?form=get_firmware_info")

    def __init__(self, session: aiohttp.ClientSession, host: str, password: str) -> None:
        super().__init__(session, host, password)
        self._key = self._iv = ""
        self._hash = hashlib.md5(password.encode()).hexdigest()  # noqa: S324 - device protocol
        self._rsa: tuple[str, str] = ("", "")
        self._seq = 0

    @property
    def headers(self) -> dict[str, str]:
        return super().headers | {"Accept": "application/json, text/javascript, */*; q=0.01"}

    def _cipher(self) -> Cipher:
        return Cipher(algorithms.AES(self._key.encode()), modes.CBC(self._iv.encode()))

    def encrypt(self, params: str) -> str:
        padder = padding.PKCS7(128).padder()
        encryptor = self._cipher().encryptor()
        data = padder.update(params.encode()) + padder.finalize()
        return base64.b64encode(encryptor.update(data) + encryptor.finalize()).decode()

    def decrypt(self, data: str) -> str:
        decryptor = self._cipher().decryptor()
        unpadder = padding.PKCS7(128).unpadder()
        raw = decryptor.update(base64.b64decode(data)) + decryptor.finalize()
        return (unpadder.update(raw) + unpadder.finalize()).decode()

    def signature(self, length: int, login: bool) -> str:
        text = f"h={self._hash}&s={self._seq + length}"
        if login:
            text = f"k={self._key}&i={self._iv}&{text}"
        return "".join(rsa_nopad(text[i : i + 64], *self._rsa) for i in range(0, len(text), 64))

    async def request(self, path: str, params: dict[str, str], login: bool = False) -> Any:
        """One API call; returns the `data` member of a successful answer."""
        query = urlencode(params)
        if path in self.PLAIN:
            answer = _json(await self._post(path, query))
        else:
            data = self.encrypt(query)
            body = urlencode({"sign": self.signature(len(data), login), "data": data})
            answer = _json(await self._post(path, body))
            if isinstance(answer.get("data"), str):
                try:
                    answer = _json(self.decrypt(answer["data"]), quote=False)
                except ValueError as err:
                    raise PowerlineError(f"{path}: cannot decrypt the answer") from err
        if answer.get("success") is False:
            code = answer.get("errorcode", answer.get("errCode", "failed"))
            raise PowerlineRejected(_endpoint(path), str(code), answer.get("data"))
        return answer.get("data")

    async def _login(self) -> None:
        self._key = self._iv = ""  # until a new session key is sent, there is nothing to close
        keys = await self.request("/login?form=keys", {"operation": "read"})
        auth = await self.request("/login?form=auth", {"operation": "read"})
        try:
            password_key = keys["password"]
            self._rsa = (auth["key"][0], auth["key"][1])
            self._seq = int(auth["seq"])
        except (KeyError, IndexError, TypeError, ValueError) as err:
            raise PowerlineError("unexpected login keys") from err
        self._key, self._iv = _digits(16), _digits(16)
        params = {
            "password": rsa_nopad(self._password, password_key[0], password_key[1]),
            "operation": "login",
        }
        try:
            await self.request("/login?form=login", params, login=True)
        except PowerlineConnectionError:
            # The adapter answered keys and auth: a login it then left unanswered may have
            # counted as a wrong password. Never let that repeat without end.
            adapter = _adapter(self._base)
            adapter.lost_logins += 1
            if adapter.lost_logins >= MAX_LOST_LOGINS:
                raise PowerlineAuthError(
                    f"{adapter.lost_logins} logins in a row left unanswered"
                ) from None
            raise
        except PowerlineRejected as err:
            self._key = self._iv = ""
            if err.code == "user conflict":
                raise PowerlineBusyError("someone else is logged in") from err
            attempts = err.data.get("attemptsAllowed") if isinstance(err.data, dict) else None
            if err.code == "exceeded max attempts" or attempts == 0:
                raise PowerlineLockedError(err.code) from err
            # Anything else counts as a rejected password: never retried on its own
            raise PowerlineAuthError(err.code) from err

    async def _logout(self) -> None:
        if not self._key:
            return
        try:
            await self.request("/logout", {"operation": "write"})
        finally:
            self._key = self._iv = ""

    async def _optional(self, path: str, params: dict[str, str]) -> Any:
        """A read some models may lack (Wi-Fi, Ethernet): None instead of failing the poll."""
        try:
            return await self.request(path, params)
        except PowerlineHTTPError as err:
            if err.status != 404:
                raise
        except PowerlineConnectionError:
            raise  # the adapter is unreachable: the whole poll fails
        except PowerlineRejected as err:
            if err.code == "timeout":
                raise  # the session expired: the whole poll fails, not just this read
            _LOGGER.debug("%s not available: %s", _endpoint(path), err.code)
            return None
        except PowerlineError as err:
            # Refused or unreadable: this model does not offer it, or not right now
            _LOGGER.debug("%s not available: %s", _endpoint(path), err)
            return None
        _LOGGER.debug("%s not available on this model", _endpoint(path))
        return None

    async def _read(self) -> PowerlineState:
        settings = await self.request(
            "/admin/ghn_powerline?form=plc_settings", {"operation": "read"}
        )
        firmware = await self.request(
            "/admin/firmware?form=get_firmware_info", {"operation": "read"}
        )
        peers = await self.request("/admin/powerline?form=plc_device", {"operation": "load"})
        led = await self.request(
            "/admin/ledSettings?form=enable", {"operation": "read", "enable": "toggle"}
        )
        wlan = await self._optional("/admin/wlan_status", {"operation": "read"})
        clients = await self._optional("/admin/wireless?form=statistics", {"operation": "load"})
        ethernet = await self._optional(
            "/admin/ethernet?form=get_ethernet_status", {"operation": "read"}
        )
        return PowerlineState(
            mac=normalize_mac(settings["mac"]),
            model=firmware.get("model") or "PGW",
            hardware=firmware.get("hardware_version"),
            firmware=firmware.get("firmware_version"),
            name=settings.get("device_name"),
            peers=[
                Peer(normalize_mac(p["device_mac"]), int(p["rx_rate"]), int(p["tx_rate"]))
                for p in peers or []
                if p.get("status", "on") == "on"
            ],
            led=str(led.get("enable")) == "1",
            wifi_2g=_on(wlan, "wireless_2g_enable"),
            wifi_5g=_on(wlan, "wireless_5g_enable"),
            wifi_clients=[
                {
                    "mac": normalize_mac(c.get("mac", "")),
                    "ip": c.get("ip", ""),
                    "name": c.get("devName", ""),
                    "band": c.get("type", ""),
                }
                for c in clients
            ]
            if clients is not None
            else None,
            ethernet=[
                {
                    "connected": port.get("status") == "connected",
                    "speed": port.get("speed"),
                    "duplex": port.get("duplex"),
                }
                for port in ethernet
            ]
            if ethernet is not None
            else None,
        )

    async def set_led(self, on: bool) -> None:
        await self._exchange(
            lambda: self.request(
                "/admin/ledSettings?form=enable",
                {"operation": "write", "enable": "toggle", "toggle": "on" if on else "off"},
            )
        )

    async def reboot(self) -> None:
        await self._exchange(
            lambda: _restarting(self.request("/admin/reboot.json", {"operation": "write"}))
        )

    async def set_wifi(self, band: str, on: bool) -> None:
        """Rewrite the band settings as the web UI does, changing only `enable`."""
        path = f"/admin/wireless?form=wireless_{band}"

        async def work() -> None:
            current = await self.request(path, {"operation": "read"})
            await self.request(path, {"operation": "write"} | wifi_settings(current, on))

        await self._exchange(work)


def _on(values: dict | None, key: str) -> bool | None:
    if not values or key not in values:
        return None
    return values[key] == "on"


async def _restarting(request) -> None:
    """A restart request may go unanswered: the adapter is already restarting."""
    try:
        await request
    except PowerlineConnectionError:
        _LOGGER.debug("No answer to the restart request: the adapter is restarting")


def wifi_settings(current: dict[str, Any], on: bool) -> dict[str, str]:
    """The fields the web UI posts for a band (its `serialize`), with `enable` changed."""
    keys = ["enable", "ssid", "hidden", "encryption", "hwmode", "htmode", "channel", "txpower"]
    encryption = current.get("encryption")
    if encryption == "psk" and "sae" in str(current.get("psk_version", "")):
        # WPA3 (psk_sae in the web UI): no cipher field
        keys += ["psk_version", "psk_key"]
    elif encryption == "psk":
        keys += ["psk_version", "psk_cipher", "psk_key"]
    elif encryption == "wep":
        keys += ["wep_mode", "wep_select", "wep_type1", "wep_format1", "wep_key1"]
    elif encryption != "none":
        # Writing back without its key fields could break the band: do not guess
        raise PowerlineError(f"unsupported Wi-Fi security {encryption!r}")
    settings = {key: str(current[key]) for key in keys if current.get(key) is not None}
    if encryption == "wep":
        settings["wep_select"] = "1"
    settings["enable"] = "on" if on else "off"
    return settings


class LegacyClient(PowerlineClient):
    """PG2400-style firmware: G.hn data model keys, session token in `_t`."""

    family = FAMILY_LEGACY
    ERRORS = {
        3: "not authenticated",
        4: "bad token or another session open",
        6: "wrong password",
        7: "locked after too many wrong passwords",
        8: "too many sessions",
        9: "out of memory",
        10: "timeout",
    }
    # Login answers that are not about the password
    BUSY = (4, 8)
    TRANSIENT = (9, 10)

    def __init__(self, session: aiohttp.ClientSession, host: str, password: str) -> None:
        super().__init__(session, host, password)
        self._token = ""

    @property
    def headers(self) -> dict[str, str]:
        return super().headers | {"Accept": "text/plain, */*; q=0.01"}

    def _pacing(self) -> float:
        # Every request, login included, waits for the previous one to be this old
        return LEGACY_PACING

    @staticmethod
    def parse(text: str) -> tuple[int, dict[str, str]]:
        lines = text.split("\r\n")
        if not lines[0].startswith("ERROR="):
            raise PowerlineError(f"unexpected answer: {text[:80]!r}")
        values = dict(line.split("=", 1) for line in lines[1:] if "=" in line)
        try:
            return int(lines[0][6:]), values
        except ValueError as err:
            raise PowerlineError(f"unexpected answer: {lines[0][:20]!r}") from err

    def _check(self, text: str) -> dict[str, str]:
        code, values = self.parse(text)
        if code:
            raise PowerlineError(self.ERRORS.get(code, f"error {code}"))
        return values

    async def _call(self, method: str, query: str = "", body: dict | None = None) -> dict[str, str]:
        path = f"/?_t={self._token}" + (f"&{query}" if query else "")
        if method == "GET":
            return self._check(await self._send("GET", path))
        return self._check(await self._post(path, urlencode(body or {})))

    async def read(self, *keys: str) -> dict[str, str]:
        return await self._call("GET", "&".join(f"{key}=" for key in keys))

    async def write(self, values: dict[str, str]) -> dict[str, str]:
        return await self._call("POST", body=values)

    async def _login(self) -> None:
        self._token = ""
        digest = hashlib.md5(self._password.encode()).hexdigest()  # noqa: S324 - device protocol
        code, values = self.parse(
            await self._post("/", urlencode({"TPLINK.GENERAL.LOGIN_PASSWORD": digest}))
        )
        if code == 0 and values.get("TOKEN"):
            self._token = values["TOKEN"]
            return
        message = self.ERRORS.get(code, f"login error {code}")
        if code in self.BUSY:
            raise PowerlineBusyError(message)
        if code in self.TRANSIENT:
            raise PowerlineError(message)
        if code == 7:
            raise PowerlineLockedError(message)
        # Wrong password, and anything unknown: never retried on its own
        raise PowerlineAuthError(message)

    async def _logout(self) -> None:
        if not self._token:
            return
        try:
            await self.write({"COMMAND": "logout"})
        finally:
            self._token = ""

    async def _read(self) -> PowerlineState:
        # Grouped exactly as the web UI asks: other groupings make the adapter drop the request
        system = await self.read(
            "SYSTEM.GENERAL.FW_VERSION",
            "SYSTEM.PRODUCTION.HW_PRODUCT",
            "SYSTEM.PRODUCTION.HW_REVISION",
        )
        node = await self.read("SYSTEM.PRODUCTION.MAC_ADDR", "NODE.GENERAL.DEVICE_NAME")
        network = await self.read(
            "DIDMNG.GENERAL.MACS", "DIDMNG.GENERAL.RX_BPS", "DIDMNG.GENERAL.TX_BPS"
        )
        led = await self.read("TPLINK.LEDSCHEDULE.LED_ENABLE")
        mac = normalize_mac(node["SYSTEM.PRODUCTION.MAC_ADDR"])
        return PowerlineState(
            mac=mac,
            model=system.get("SYSTEM.PRODUCTION.HW_PRODUCT") or "PG",
            hardware=system.get("SYSTEM.PRODUCTION.HW_REVISION"),
            firmware=system.get("SYSTEM.GENERAL.FW_VERSION"),
            name=node.get("NODE.GENERAL.DEVICE_NAME"),
            peers=legacy_peers(network, mac),
            led=led.get("TPLINK.LEDSCHEDULE.LED_ENABLE") == "YES",
        )

    async def set_led(self, on: bool) -> None:
        await self._exchange(
            lambda: self.write({"TPLINK.LEDSCHEDULE.LED_ENABLE": "YES" if on else "NO"})
        )

    async def reboot(self) -> None:
        await self._exchange(lambda: _restarting(self.write({"SYSTEM.GENERAL.HW_RESET": "1"})))


def legacy_peers(network: dict[str, str], own_mac: str) -> list[Peer]:
    """Peers from the comma-separated DIDMNG lists, without the empty slot and the adapter."""
    macs = network.get("DIDMNG.GENERAL.MACS", "").split(",")
    rx = network.get("DIDMNG.GENERAL.RX_BPS", "").split(",")
    tx = network.get("DIDMNG.GENERAL.TX_BPS", "").split(",")
    peers = []
    for index, raw in enumerate(macs):
        mac = normalize_mac(raw)
        if not raw or mac in (NO_PEER, own_mac) or index >= min(len(rx), len(tx)):
            continue
        try:
            rates = int(rx[index]) // LEGACY_RATE_DIVISOR, int(tx[index]) // LEGACY_RATE_DIVISOR
        except ValueError:
            continue
        peers.append(Peer(mac, *rates))
    return peers


async def detect(session: aiohttp.ClientSession, host: str, password: str) -> PowerlineClient:
    """The client for this adapter's firmware, found with requests that need no login.

    Holds the adapter like an exchange does, so the probes never land inside a poll's session.
    """
    encrypted = EncryptedClient(session, host, password)
    legacy = LegacyClient(session, host, password)
    async with encrypted.lock:
        try:
            answer = _json(await encrypted._post("/admin/locale?form=index_lang", "operation=read"))
            if answer.get("success"):
                return encrypted
        except PowerlineError:
            pass
        # Paced after the first probe: requests share the adapter's timestamp
        try:
            legacy.parse(await legacy._post("/", "COMMAND=is+factorydefault"))
        except PowerlineError as err:
            raise PowerlineError(f"{host} is not a supported TP-Link powerline adapter") from err
    return legacy


def client_for(
    session: aiohttp.ClientSession, family: str, host: str, password: str
) -> PowerlineClient:
    cls = EncryptedClient if family == FAMILY_ENCRYPTED else LegacyClient
    return cls(session, host, password)
