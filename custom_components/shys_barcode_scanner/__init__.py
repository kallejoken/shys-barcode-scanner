"""SHYS Barcode Scanner: selbstlernende Barcode-Datenbank für Home Assistant."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_ADD_TO_LIST,
    ATTR_BRAND,
    ATTR_DATA,
    ATTR_DEVICE,
    ATTR_EAN,
    ATTR_MERGE,
    ATTR_NAME,
    ATTR_TARGET_LIST,
    CATALOG_DB_FILE,
    CONF_CATALOG_BASE_URL,
    CONF_USER_AGENT,
    DEFAULT_USER_AGENT,
    DOMAIN,
    PANEL_COMPONENT,
    PANEL_ICON,
    PANEL_JS_FILE,
    PANEL_STATIC_URL,
    PANEL_TITLE,
    PANEL_URL_PATH,
    SERVICE_DELETE_PRODUCT,
    SERVICE_DISMISS_PENDING,
    SERVICE_EXPORT_DATABASE,
    SERVICE_IMPORT_DATABASE,
    SERVICE_LEARN_PRODUCT,
    SERVICE_SCAN,
    STORAGE_KEY,
    STORAGE_VERSION,
    VERSION,
)
from .manager import BarcodeManager
from .offline_catalog import OfflineCatalog
from .storage import BarcodeStorage
from .websocket import async_register_websocket_commands

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

DATA_STATIC_REGISTERED = f"{DOMAIN}_static_registered"

SCAN_SCHEMA = vol.Schema(
    {vol.Required(ATTR_EAN): cv.string, vol.Optional(ATTR_DEVICE): cv.string}
)
LEARN_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_EAN): cv.string,
        vol.Required(ATTR_NAME): cv.string,
        vol.Optional(ATTR_BRAND): cv.string,
        vol.Optional(ATTR_TARGET_LIST): vol.Any(None, "", cv.entity_domain("todo")),
        vol.Optional(ATTR_ADD_TO_LIST, default=False): cv.boolean,
    }
)
EAN_SCHEMA = vol.Schema({vol.Required(ATTR_EAN): cv.string})
IMPORT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DATA): vol.Any(dict, cv.string),
        vol.Optional(ATTR_MERGE, default=True): cv.boolean,
    }
)


def get_manager(hass: HomeAssistant) -> BarcodeManager:
    """Aktiven Manager holen oder verständlichen Fehler werfen."""
    manager: BarcodeManager | None = hass.data.get(DOMAIN)
    if manager is None:
        raise ServiceValidationError("SHYS Barcode Scanner ist nicht eingerichtet")
    return manager


def parse_import_data(data: Any) -> dict[str, Any]:
    """Import-Daten (dict oder JSON-Text) in ein dict wandeln."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError as err:
            raise ServiceValidationError(f"Ungültiges JSON: {err}") from err
    if not isinstance(data, dict):
        raise ServiceValidationError("Import muss ein JSON-Objekt sein")
    return data


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Services und WebSocket-Befehle einmalig registrieren."""

    async def handle_scan(call: ServiceCall) -> ServiceResponse:
        return await get_manager(hass).async_handle_scan(
            call.data[ATTR_EAN], device=call.data.get(ATTR_DEVICE)
        )

    async def handle_learn(call: ServiceCall) -> ServiceResponse:
        try:
            result = await get_manager(hass).async_learn(
                call.data[ATTR_EAN],
                call.data[ATTR_NAME],
                brand=call.data.get(ATTR_BRAND),
                target_list=call.data.get(ATTR_TARGET_LIST) or None,
                add_to_list=call.data[ATTR_ADD_TO_LIST],
            )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        return result

    async def handle_delete(call: ServiceCall) -> None:
        await get_manager(hass).async_delete(call.data[ATTR_EAN])

    async def handle_dismiss(call: ServiceCall) -> None:
        await get_manager(hass).async_dismiss(call.data[ATTR_EAN])

    async def handle_export(call: ServiceCall) -> ServiceResponse:
        return get_manager(hass).storage.export_dict()

    async def handle_import(call: ServiceCall) -> ServiceResponse:
        data = parse_import_data(call.data[ATTR_DATA])
        try:
            return await get_manager(hass).async_import(data, merge=call.data[ATTR_MERGE])
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    hass.services.async_register(
        DOMAIN, SERVICE_SCAN, handle_scan, SCAN_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, SERVICE_LEARN_PRODUCT, handle_learn, LEARN_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, SERVICE_DELETE_PRODUCT, handle_delete, EAN_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DISMISS_PENDING, handle_dismiss, EAN_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_EXPORT_DATABASE, handle_export, vol.Schema({}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_IMPORT_DATABASE, handle_import, IMPORT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    async_register_websocket_commands(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Integration aus dem Config-Entry starten."""
    storage = BarcodeStorage(Store(hass, STORAGE_VERSION, STORAGE_KEY))
    await storage.async_load()
    config = {**entry.data, **entry.options}
    session = async_get_clientsession(hass)
    # Eigene SQLite-Datei neben configuration.yaml – bewusst nicht im .storage-JSON
    catalog = OfflineCatalog(
        hass,
        hass.config.path(CATALOG_DB_FILE),
        session,
        base_url=config.get(CONF_CATALOG_BASE_URL) or None,
        user_agent=config.get(CONF_USER_AGENT) or DEFAULT_USER_AGENT,
    )
    manager = BarcodeManager(hass, config, storage, session, catalog=catalog)
    manager.async_start()
    hass.data[DOMAIN] = manager

    await _async_register_panel(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Optionen live übernehmen (ohne Neustart)."""
    manager: BarcodeManager | None = hass.data.get(DOMAIN)
    if manager is not None:
        manager.update_config({**entry.data, **entry.options})
        manager.async_notify_listeners()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Integration entladen."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        manager: BarcodeManager | None = hass.data.pop(DOMAIN, None)
        if manager is not None:
            manager.async_stop()
            if manager.catalog is not None:
                await manager.catalog.async_close()
        frontend.async_remove_panel(hass, PANEL_URL_PATH)
    return unloaded


async def _async_register_panel(hass: HomeAssistant) -> None:
    """Seitenleisten-Panel registrieren (statischer Pfad nur einmal pro HA-Lauf)."""
    if not hass.data.get(DATA_STATIC_REGISTERED):
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    PANEL_STATIC_URL, str(Path(__file__).parent / "frontend"), cache_headers=False
                )
            ]
        )
        hass.data[DATA_STATIC_REGISTERED] = True

    if PANEL_URL_PATH in hass.data.get(frontend.DATA_PANELS, {}):
        return
    await panel_custom.async_register_panel(
        hass,
        webcomponent_name=PANEL_COMPONENT,
        frontend_url_path=PANEL_URL_PATH,
        module_url=f"{PANEL_STATIC_URL}/{PANEL_JS_FILE}?v={VERSION}",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        require_admin=False,
        config={},
    )
