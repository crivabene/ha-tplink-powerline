"""Constants for the TP-Link powerline integration."""

from datetime import timedelta

DOMAIN = "tplink_powerline"
CONF_FAMILY = "family"
# Set when the adapter rejected the stored password; cleared by reauth or reconfigure
CONF_REJECTED = "rejected"

# Each poll is a full login/read/logout: once a minute is plenty for link rates
POLL_INTERVAL = timedelta(seconds=60)
# Re-read this soon after a command, instead of waiting for the next poll
CONFIRM_DELAY = 5  # seconds

# Option: look up the latest firmware on TP-Link's download page (adapters that cannot
# check by themselves); off unless the user turns it on
CONF_WEBSITE_CHECK = "website_check"
# Firmware is rarely released: checking twice a day is plenty, adapter and website alike
FIRMWARE_INTERVAL = timedelta(hours=12)
# After a failed check (web UI in use, adapter restarting, website down)
FIRMWARE_RETRY = timedelta(minutes=15)
