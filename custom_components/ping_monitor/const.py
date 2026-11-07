"""Constants for the Ping Monitor integration."""

from typing import Final

DOMAIN: Final = "ping_monitor"

CONF_PAYLOAD_SIZE: Final = "payload_size"
CONF_INTERVAL: Final = "interval"
CONF_TIMEOUT: Final = "timeout"
CONF_WINDOW: Final = "window"
CONF_PUBLISH_INTERVAL: Final = "publish_interval"
CONF_TTL: Final = "ttl"
CONF_DOWN_AFTER: Final = "down_after"
CONF_UP_AFTER: Final = "up_after"

DEFAULTS: Final = {
    CONF_PAYLOAD_SIZE: 56,
    CONF_INTERVAL: 1.0,
    CONF_TIMEOUT: 2.0,
    CONF_WINDOW: 300,
    CONF_PUBLISH_INTERVAL: 10,
    CONF_TTL: 64,
    CONF_DOWN_AFTER: 5,
    CONF_UP_AFTER: 3,
}

EVENT_OUTAGE_STARTED: Final = f"{DOMAIN}_outage_started"
EVENT_OUTAGE_ENDED: Final = f"{DOMAIN}_outage_ended"
