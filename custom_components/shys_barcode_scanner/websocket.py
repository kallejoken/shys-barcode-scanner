"""WebSocket-Befehle für das Seitenleisten-Panel."""

from __future__ import annotations

import json
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN


def _manager(hass: HomeAssistant, connection: Any, msg: dict[str, Any]):
    manager = hass.data.get(DOMAIN)
    if manager is None:
        connection.send_error(msg["id"], "not_loaded", "SHYS Barcode Scanner ist nicht eingerichtet")
    return manager


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get_state"})
@callback
def ws_get_state(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if (manager := _manager(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], manager.get_state())


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/learn",
        vol.Required("ean"): str,
        vol.Required("name"): str,
        vol.Optional("brand"): vol.Any(None, str),
        vol.Optional("target_list"): vol.Any(None, str),
        vol.Optional("add_to_list", default=False): bool,
    }
)
@websocket_api.async_response
async def ws_learn(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if (manager := _manager(hass, connection, msg)) is None:
        return
    target = msg.get("target_list") or None
    if target and not target.startswith("todo."):
        connection.send_error(msg["id"], "invalid_format", "Ziel muss eine todo.*-Liste sein")
        return
    try:
        result = await manager.async_learn(
            msg["ean"], msg["name"], brand=msg.get("brand"), target_list=target,
            add_to_list=msg["add_to_list"],
        )
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/delete", vol.Required("ean"): str})
@websocket_api.async_response
async def ws_delete(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if (manager := _manager(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], {"removed": await manager.async_delete(msg["ean"])})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/dismiss_pending", vol.Required("ean"): str}
)
@websocket_api.async_response
async def ws_dismiss(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if (manager := _manager(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], {"removed": await manager.async_dismiss(msg["ean"])})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/scan", vol.Required("ean"): str, vol.Optional("device"): str}
)
@websocket_api.async_response
async def ws_scan(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if (manager := _manager(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], await manager.async_handle_scan(msg["ean"], device=msg.get("device")))


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/export"})
@callback
def ws_export(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if (manager := _manager(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], manager.storage.export_dict())


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/import",
        vol.Required("data"): vol.Any(dict, str),
        vol.Optional("merge", default=True): bool,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_import(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if (manager := _manager(hass, connection, msg)) is None:
        return
    data = msg["data"]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError as err:
            connection.send_error(msg["id"], "invalid_format", f"Ungültiges JSON: {err}")
            return
    try:
        stats = await manager.async_import(data, merge=msg["merge"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    connection.send_result(msg["id"], stats)


@callback
def async_register_websocket_commands(hass: HomeAssistant) -> None:
    for handler in (ws_get_state, ws_learn, ws_delete, ws_dismiss, ws_scan, ws_export, ws_import):
        websocket_api.async_register_command(hass, handler)
