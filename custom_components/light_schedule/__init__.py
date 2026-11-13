"""Licht-Zeitsteuerung (light_schedule) – beliebig viele Profile je Raum, bearbeitet im Raum-Panel
(Einstellungen → Lichtsteuerung → Zeitsteuerung). Profile liegen in .storage/light_schedule.

configuration.yaml:

    light_schedule:
      rooms: [wz, bu, ku, sz, fl, ke, ba, bz]   # optional, das ist der Standard
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.typing import ConfigType

from . import websocket
from .const import CONF_ROOMS, DEFAULT_ROOMS, DOMAIN
from .manager import ScheduleManager

CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Any(None, vol.Schema({
    vol.Optional(CONF_ROOMS, default=DEFAULT_ROOMS): vol.All(cv.ensure_list, [cv.slug]),
}))}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    conf = config.get(DOMAIN) or {}
    mgr = ScheduleManager(hass, conf.get(CONF_ROOMS) or DEFAULT_ROOMS)
    hass.data[DOMAIN] = mgr
    websocket.async_register(hass)
    await mgr.async_start()

    async def set_enabled(call: ServiceCall) -> None:
        pid = call.data["profile_id"]
        if pid not in mgr.profiles:
            raise ServiceValidationError(f"Profil {pid} nicht gefunden")
        await mgr.async_save({"id": pid, "room": mgr.profiles[pid]["room"], "enabled": call.data["enabled"]})

    hass.services.async_register(DOMAIN, "set_enabled", set_enabled, vol.Schema({
        vol.Required("profile_id"): cv.string, vol.Required("enabled"): cv.boolean}))
    hass.async_create_task(async_load_platform(hass, "sensor", DOMAIN, {}, config))
    return True
