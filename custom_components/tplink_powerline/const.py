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
