"""Fixtures: fake adapters behind the HTTP mock, and config entries pointing at them."""

from datetime import timedelta
import re
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.tplink_powerline.api import FAMILY_ENCRYPTED, FAMILY_LEGACY
from custom_components.tplink_powerline.const import CONF_FAMILY, DOMAIN, POLL_INTERVAL

from . import fixtures


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def no_pacing():
    with patch("custom_components.tplink_powerline.api.LEGACY_PACING", 0):
        yield


@pytest.fixture
def serve(aioclient_mock):
    """Answer requests to `host` with a fake adapter (also used to move one to a new IP)."""

    def _serve(device, host: str) -> None:
        pattern = re.compile(rf"http://{re.escape(host)}/.*")
        aioclient_mock.post(pattern, side_effect=device)
        aioclient_mock.get(pattern, side_effect=device)

    return _serve


@pytest.fixture
def encrypted_device(serve):
    device = fixtures.EncryptedDevice()
    serve(device, fixtures.ENCRYPTED_HOST)
    return device


@pytest.fixture
def legacy_device(serve):
    device = fixtures.LegacyDevice()
    serve(device, fixtures.LEGACY_HOST)
    return device


def _entry(hass, host: str, mac: str, family: str) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": host, "password": fixtures.PASSWORD, CONF_FAMILY: family},
        unique_id=mac,
        title=host,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def encrypted_entry_factory(hass, encrypted_device, freezer):
    """Set up the encrypted adapter's entry once the test has prepared the fake."""

    async def _setup() -> MockConfigEntry:
        entry = _entry(hass, fixtures.ENCRYPTED_HOST, fixtures.ENCRYPTED_MAC, FAMILY_ENCRYPTED)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry

    return _setup


@pytest.fixture
async def encrypted_entry(encrypted_entry_factory):
    return await encrypted_entry_factory()


@pytest.fixture
async def legacy_entry(hass, legacy_device, freezer):
    entry = _entry(hass, fixtures.LEGACY_HOST, fixtures.LEGACY_MAC, FAMILY_LEGACY)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
def advance(hass, freezer):
    """Move the clock forward and run whatever was due."""

    async def _advance(delta: timedelta) -> None:
        freezer.tick(delta)
        async_fire_time_changed(hass)
        # Scheduled polls run as background tasks
        await hass.async_block_till_done(wait_background_tasks=True)

    return _advance


@pytest.fixture
def next_poll(advance):
    async def _next_poll() -> None:
        await advance(POLL_INTERVAL + timedelta(seconds=1))

    return _next_poll
