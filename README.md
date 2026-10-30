# TP-Link Powerline (G.hn) for Home Assistant

Local integration for TP-Link **G.hn powerline adapters**: the link rate to every other
adapter, LEDs, restart and, on Wi-Fi models, each Wi-Fi band. It talks to the adapter's own
web page over the LAN: no cloud, no TP-Link ID, no extra Python packages.

> Not affiliated with or endorsed by TP-Link. "TP-Link" is a trademark of its owner.

## Supported adapters

| Model | Firmware tested | Firmware family | Notes |
|---|---|---|---|
| PGW2440 v1.0 | 1.0.4 Build 20240830 | encrypted | Wi-Fi extender model |
| PG2400P v1.0 | 1.0.3 Build 20221213 | legacy | |

Two firmware families exist and the integration detects which one an adapter runs:

- **encrypted**: the TP-Link router-style web UI (requests are AES-encrypted and RSA-signed);
- **legacy**: an older web UI over the G.hn data model (`DIDMNG.GENERAL.MACS`, ...).

Other G.hn models (PG2400, PGW2440 kits, PG1200, ...) probably use one of the two. If yours
works, or does not, please open an issue with the model, hardware and firmware version.
HomePlug AV adapters (TL-PA...) speak another protocol and are not supported.

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=crivabene&repository=ha-tplink-powerline&category=integration)

Or: HACS → ⋮ → Custom repositories → add `https://github.com/crivabene/ha-tplink-powerline`
with category *Integration*. Then download **TP-Link Powerline (G.hn)** and restart Home
Assistant. HACS will offer updates when a new release comes out.

### Manual

1. Download `tplink_powerline.zip` from the
   [latest release](https://github.com/crivabene/ha-tplink-powerline/releases/latest).
2. Extract it into `config/custom_components/tplink_powerline/` (create the folders if
   needed), so that `config/custom_components/tplink_powerline/manifest.json` exists.
3. Restart Home Assistant.

## Configuration

[![Open your Home Assistant instance and start setting up TP-Link Powerline (G.hn).](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=tplink_powerline)

Or: Settings → Devices & services → **Add integration** → **TP-Link Powerline (G.hn)**, then
enter:

- **Host**: the adapter's IP address. Give it a DHCP reservation so it does not change.
- **Password**: the password of the adapter's web page.

Add each adapter on its own: every adapter only knows its own settings. The address and the
password can be changed later with *Reconfigure*.

## Entities

| Entity | Type | Models | Notes |
|---|---|---|---|
| Receive rate from *peer* | sensor | all | Mbit/s, one per other adapter on the network |
| Transmit rate to *peer* | sensor | all | Mbit/s |
| Connected adapters | sensor | all | other adapters seen over the mains |
| Powerline link | binary sensor | all | on while at least one other adapter is reachable |
| LEDs | switch | all | |
| Restart | button | all | the powerline link drops for about a minute |
| Wi-Fi 2.4 GHz / 5 GHz | switch | Wi-Fi models | turning a band off disconnects its clients |
| Wi-Fi clients | sensor | Wi-Fi models | |
| Ethernet | binary sensor | models that report it (PGW2440) | link state; speed and duplex as attributes |

Peers are named after their MAC address: rename the entities as you like. The rate
sensors of a peer become unavailable when it leaves the network, and new peers get their
own sensors as soon as they appear.

### Dashboard example

A card for an adapter named *Garage* with Wi-Fi, linked to one other adapter: the link and
its rates, the Wi-Fi band, the LEDs, a warning when no other adapter is reachable and the
link rate over the last week. In a dashboard: **Edit → Add card → Manual**, then paste:

```yaml
type: vertical-stack
cards:
  - type: heading
    heading: Powerline
  - type: grid
    columns: 2
    square: false
    cards:
      - type: tile
        entity: binary_sensor.garage_powerline_link
        name: Link
      - type: tile
        entity: sensor.garage_wi_fi_clients
        name: Wi-Fi clients
      - type: tile
        entity: sensor.garage_receive_rate_from_aa_bb_cc_dd_ee_01
        name: Receive
      - type: tile
        entity: sensor.garage_transmit_rate_to_aa_bb_cc_dd_ee_01
        name: Transmit
      - type: tile
        entity: switch.garage_wi_fi_2_4_ghz
        name: Wi-Fi 2.4 GHz
        features:
          - type: toggle
      - type: tile
        entity: switch.garage_leds
        name: LEDs
        features:
          - type: toggle
  - type: conditional
    conditions:
      - condition: state
        entity: binary_sensor.garage_powerline_link
        state: "off"
    card:
      type: markdown
      content: ⚠️ **No other adapter is reachable over the mains.**
  - type: history-graph
    title: Link rate, last 7 days
    hours_to_show: 168
    entities:
      - entity: sensor.garage_receive_rate_from_aa_bb_cc_dd_ee_01
        name: Receive
      - entity: sensor.garage_transmit_rate_to_aa_bb_cc_dd_ee_01
        name: Transmit
```

Entity IDs follow the adapter's name (as set on its web page), the other adapter's MAC
address and the language Home Assistant had when the adapter was added. The example uses
English ones and the made-up MAC `AA:BB:CC:DD:EE:01`; find yours under
**Settings → Devices & services → TP-Link Powerline (G.hn) → device**, or pick them in the
card editor. On an adapter without Wi-Fi, drop the two Wi-Fi tiles.

| English | Italian |
|---|---|
| `binary_sensor.garage_powerline_link` | `binary_sensor.garage_collegamento_powerline` |
| `sensor.garage_receive_rate_from_<mac>` | `sensor.garage_velocita_in_ricezione_da_<mac>` |
| `sensor.garage_transmit_rate_to_<mac>` | `sensor.garage_velocita_in_trasmissione_verso_<mac>` |
| `sensor.garage_connected_adapters` | `sensor.garage_adattatori_collegati` |
| `sensor.garage_wi_fi_clients` | `sensor.garage_client_wi_fi` |
| `switch.garage_wi_fi_2_4_ghz` | `switch.garage_wi_fi_24_ghz` |
| `switch.garage_wi_fi_5_ghz` | `switch.garage_wi_fi_5_ghz` |
| `switch.garage_leds` | `switch.garage_led` |
| `button.garage_restart` | `button.garage_riavvia` |

## How it behaves

- **Polling**: once a minute. Each poll logs in, reads and logs out, like the web page
  does; polls, commands and setup take turns, never talking to the same adapter at once.
  The adapters accept one admin session at a time, so the web page stays usable in a
  browser; while you are logged in there, polls fail and the entities show unavailable
  until you log out.
- **Wrong password**: the adapters lock their web page after about ten wrong passwords.
  When a password is rejected, or a login is refused for a reason the integration does not
  know, it stops logging in at once (also across restarts) and asks for the password again
  (re-authentication) instead of retrying. Three logins in a row left unanswered by an
  adapter that answers everything else are treated the same way, since each may have
  counted as a wrong attempt.
- **Wi-Fi switches** rewrite the band settings exactly as the web page does, changing only
  the on/off flag.

## Languages

English, Italian, German, French, Spanish, Portuguese, Dutch and Polish. Home Assistant picks
the one matching its language setting. Apart from English and Italian the translations have
not been reviewed by a native speaker: corrections are welcome (see
[CONTRIBUTING.md](CONTRIBUTING.md#translations)).

## Diagnostics

Device page → ⋮ → Download diagnostics. Passwords, MAC and IP addresses are removed.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python -m pytest
```

The tests run the clients against fake adapters that check the protocol: RSA signatures
and AES data, session tokens, a single admin session, the key groups the legacy firmware
accepts and the browser headers. They do not replace a try on a real adapter.

## Contributing

Another adapter model, a bug or a translation: see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
