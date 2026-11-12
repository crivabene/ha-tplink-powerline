"""Latest firmware listed on TP-Link's download page for a model: a check only, nothing is
downloaded. Used when the user turns it on, for adapters that cannot check by themselves.

The download pages list each firmware as e.g. `PG2400P KIT(EU)_V1_1.1.0 Build 20250710`
(model, region, hardware version, firmware version, build date).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re

import aiohttp

from .api import BROWSER_HEADERS, TIMEOUT, PowerlineError

_LOGGER = logging.getLogger(__name__)

PAGE = "https://www.tp-link.com/{region}/support/download/{slug}-kit/v{hardware}/"
# Firmware is only published on one regional site for some models
REGIONS = {"PG2405P": "fr"}
DEFAULT_REGION = "en"
# Models are short codes (PG2400P, PGW2440, PG1200): anything else is not looked up
MODEL = re.compile(r"PGW?\d{4}P?")


@dataclass(frozen=True)
class WebsiteFirmware:
    version: str  # "1.1.0 Build 20250710"
    url: str  # the download page


def hardware_major(text: str | None) -> int | None:
    """1 from "1.0" (legacy) or "PGW2440 v1.0" (encrypted)."""
    match = re.search(r"(\d+)(?:\.\d+)*\s*$", text or "")
    return int(match[1]) if match else None


def page_url(model: str, hardware: str | None) -> str | None:
    major = hardware_major(hardware)
    if not MODEL.fullmatch(model or "") or major is None:
        return None
    return PAGE.format(
        region=REGIONS.get(model, DEFAULT_REGION), slug=model.lower(), hardware=major
    )


def latest_on_page(html: str, model: str, hardware: str | None) -> str | None:
    """Newest firmware of this model and hardware version listed in the page."""
    major = hardware_major(hardware)
    pattern = re.compile(
        rf"\b{re.escape(model)}(?: KIT)?\([A-Z]+\)_V{major}_(\d+)\.(\d+)\.(\d+) Build (\d{{8}})"
    )
    found = {tuple(int(part) for part in match.groups()) for match in pattern.finditer(html)}
    if not found:
        return None
    newest = max(found)
    return f"{newest[0]}.{newest[1]}.{newest[2]} Build {newest[3]}"


async def latest_firmware(
    session: aiohttp.ClientSession, model: str, hardware: str | None
) -> WebsiteFirmware | None:
    """None when the model has no page or the page lists no firmware for it."""
    url = page_url(model, hardware)
    if url is None:
        _LOGGER.debug("No TP-Link download page known for %s %s", model, hardware)
        return None
    try:
        async with session.get(
            url, headers={"User-Agent": BROWSER_HEADERS["User-Agent"]}, timeout=TIMEOUT
        ) as response:
            if response.status == 404:
                return None
            if response.status != 200:
                raise PowerlineError(f"TP-Link download page: HTTP {response.status}")
            html = await response.text()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise PowerlineError(f"TP-Link download page: {type(err).__name__}") from err
    version = latest_on_page(html, model, hardware)
    if version is None:
        _LOGGER.debug("No %s firmware listed on %s", model, url)
        return None
    return WebsiteFirmware(version, url)
