"""Config and options flow for Ping Monitor."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)
import voluptuous as vol

from .const import (
    CONF_DOWN_AFTER,
    CONF_INTERVAL,
    CONF_PAYLOAD_SIZE,
    CONF_PUBLISH_INTERVAL,
    CONF_TIMEOUT,
    CONF_TTL,
    CONF_UP_AFTER,
    CONF_WINDOW,
    DEFAULTS,
    DOMAIN,
)
from .icmp import MAX_PAYLOAD_SIZE, IcmpSocket, IcmpSocketError


def _num(lo: float, hi: float, step: float = 1, unit: str | None = None) -> NumberSelector:
    config = NumberSelectorConfig(min=lo, max=hi, step=step, mode=NumberSelectorMode.BOX)
    if unit:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


def _options_schema(current: dict[str, Any]) -> dict[vol.Marker, Any]:
    def opt(key: str) -> vol.Required:
        return vol.Required(key, default=current.get(key, DEFAULTS[key]))

    return {
        opt(CONF_PAYLOAD_SIZE): _num(0, MAX_PAYLOAD_SIZE, unit="B"),
        opt(CONF_INTERVAL): _num(0.2, 60, 0.1, "s"),
        opt(CONF_TIMEOUT): _num(0.1, 10, 0.1, "s"),
        opt(CONF_WINDOW): _num(10, 3600, unit="s"),
        opt(CONF_PUBLISH_INTERVAL): _num(1, 300, unit="s"),
        opt(CONF_TTL): _num(1, 255),
        opt(CONF_DOWN_AFTER): _num(1, 100),
        opt(CONF_UP_AFTER): _num(1, 100),
    }


def _normalize(user_input: dict[str, Any]) -> dict[str, Any]:
    """Number selectors return floats; keep integers as integers."""
    out = {}
    for key, default in DEFAULTS.items():
        value = user_input[key]
        out[key] = int(value) if isinstance(default, int) else float(value)
    return out


def _validate_options(options: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if options[CONF_WINDOW] < 2 * options[CONF_INTERVAL]:
        errors[CONF_WINDOW] = "window_too_short"
    elif options[CONF_WINDOW] <= options[CONF_TIMEOUT]:
        errors[CONF_WINDOW] = "window_shorter_than_timeout"
    if options[CONF_PUBLISH_INTERVAL] > options[CONF_WINDOW]:
        errors[CONF_PUBLISH_INTERVAL] = "publish_longer_than_window"
    return errors


async def _validate_host(hass: HomeAssistant, host: str) -> str | None:
    """Return an error key, or None if the host is usable."""
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        try:
            await hass.loop.getaddrinfo(host, None, family=socket.AF_INET)
        except OSError:
            return "cannot_resolve"
    try:
        IcmpSocket.open().close()
    except IcmpSocketError:
        return "socket_not_permitted"
    return None


class PingMonitorConfigFlow(ConfigFlow, domain=DOMAIN):
    """One config entry per monitored target."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add a target."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            options = _normalize(user_input)
            errors = _validate_options(options)
            if not errors and (error := await _validate_host(self.hass, host)):
                errors[CONF_HOST] = error
            if not errors:
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME) or host,
                    data={CONF_HOST: host},
                    options=options,
                )

        current = user_input or {}
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=current.get(CONF_HOST, "")): TextSelector(),
                vol.Optional(CONF_NAME, default=current.get(CONF_NAME, "")): TextSelector(),
                **_options_schema(current),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PingMonitorOptionsFlow:
        """Return the options flow."""
        return PingMonitorOptionsFlow()


class PingMonitorOptionsFlow(OptionsFlow):
    """Change probing and statistics parameters of a target."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Edit options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            options = _normalize(user_input)
            errors = _validate_options(options)
            if not errors:
                return self.async_create_entry(data=options)
        current = user_input or dict(self.config_entry.options)
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(_options_schema(current)), errors=errors
        )
