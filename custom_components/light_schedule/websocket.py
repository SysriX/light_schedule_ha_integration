"""WebSocket-API für das Raum-Panel (Reiter Zeitsteuerung)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN


@callback
def async_register(hass: HomeAssistant) -> None:
    for cmd in (ws_save, ws_delete, ws_test):
        websocket_api.async_register_command(hass, cmd)


async def _run(connection, msg, coro) -> None:
    try:
        res = await coro
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    connection.send_result(msg["id"], {"result": res})


@websocket_api.websocket_command({vol.Required("type"): "light_schedule/save", vol.Required("profile"): dict})
@websocket_api.async_response
async def ws_save(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, hass.data[DOMAIN].async_save(msg["profile"]))


@websocket_api.websocket_command({vol.Required("type"): "light_schedule/delete", vol.Required("profile_id"): str})
@websocket_api.async_response
async def ws_delete(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, hass.data[DOMAIN].async_delete(msg["profile_id"]))


@websocket_api.websocket_command({vol.Required("type"): "light_schedule/test", vol.Required("profile"): dict})
@websocket_api.async_response
async def ws_test(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, hass.data[DOMAIN].async_test(msg["profile"]))
