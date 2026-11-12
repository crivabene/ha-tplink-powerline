# Contributing

Thanks for helping! The most useful contribution is **support for another G.hn adapter
model**, and you do not need to write code for it: diagnostics and a few requests copied
from your browser are enough to get started.

## The one rule

Every request must be **one the adapter's own web page sends** for the same action, with the
same grouping of fields, and nothing more.

The adapters' web servers are fragile: the legacy firmware drops the connection when keys are
grouped differently from the web page, the encrypted firmware resets requests that do not
look like a browser's, and both lock their web page after about ten wrong passwords. So this
integration never guesses a request: it copies one the web page was seen sending.

Some web page actions must never be automated: unpairing an adapter (`plc remove`), pairing,
factory reset, password change, and **uploading a firmware file**. The only firmware upgrade
offered is the web page's *online* upgrade (encrypted firmware 1.1.0+), where the adapter
downloads and checks the firmware itself; requests that firmware page sends are never sent
to older firmware, which drops the connection on them.

## Adding an adapter model

### 1. Check what works already

Install the integration and add the adapter. It detects which of the two known firmware
families the adapter runs (shown in the diagnostics as `family`), and entities are only
created for what the adapter reports. Many models of the same generation will just work.

If the adapter is rejected with "Could not reach a supported adapter", or something is
missing or wrong, go on.

### 2. Record what the web page sends

1. Open the adapter's web page in Chrome, Edge or Firefox and open the developer tools
   (F12) on the **Network** tab, with **Preserve log** on.
2. Log in, then do **one action at a time**: open the page that shows the information you
   want (e.g. the network map with the link rates), or change one setting (e.g. LEDs off).
3. Right-click the request that carried it → **Copy → Copy as cURL**, and note what you did.

**Before pasting anything**, replace the password (or its hash), the session token (`_t=`,
`stok`), cookies and Wi-Fi keys with `XXX`.

On the encrypted firmware the requests are encrypted (`sign=...&data=...`), so copying them
does not help; the field names are in the web page's JavaScript (`modules/**/models.js`).
Note the page and the action, and attach the diagnostics.

### 3. Open an issue

Use the **New adapter model** form with the model, hardware and firmware version, the
diagnostics and the requests you copied.

### 4. Or open a pull request

- `api.py`: `EncryptedClient` and `LegacyClient` hold the requests of each family; `detect`
  tells the families apart with requests that need no login.
- `website.py`: the optional firmware check on TP-Link's download pages (model → page).
- `tests/tplink_powerline/fixtures.py`: the fake adapters check the protocol (signatures,
  tokens, key groups). Add your model's answers there, **anonymised** (made-up MAC and IP
  addresses, names and passwords), plus tests asserting the exact requests sent.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Tests never talk to a real adapter. CI also runs Home Assistant's `hassfest` and the HACS
validation; all checks must pass.

Please keep:

- **English** for code, comments, logs and docs.
- **Translations** in sync: see below.
- **Load on the adapter** low: no polling faster than once a minute, one session at a time,
  always logged out.
- **Personal data** out of the repository: no real MAC or IP addresses, names, passwords or
  network names in fixtures, issues or logs.

## Translations

`custom_components/tplink_powerline/translations/en.json` is the source. Every other language
file must have exactly the same keys and the same `{placeholders}`; a test checks this for all
files.

- **Corrections**: just edit the file. Apart from English and Italian, the translations were
  written without a native speaker's review, so improvements are very welcome.
- **New language**: copy `en.json` to `<language code>.json` (the code Home Assistant uses,
  e.g. `sv`, `cs`) and translate the values, never the keys or the `{placeholders}`.

## Reporting bugs

Use the **Bug report** form, with diagnostics and, if possible, a debug log:

```yaml
logger:
  logs:
    custom_components.tplink_powerline: debug
```
