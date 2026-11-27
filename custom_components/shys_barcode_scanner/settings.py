"""Lesen, Prüfen und Speichern der Integrations-Optionen für das Panel."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .config_flow import (
    TEXT_OPTION_KEYS,
    available_notify_services,
    normalize_options,
    validate_notify_services,
)
from .const import (
    CONF_CATALOG_BASE_URL,
    CONF_DUPLICATE_MODE,
    CONF_ESPHOME_DEVICE_PREFIX,
    CONF_NAME_FORMAT,
    CONF_NOTIFY_ON_SUCCESS,
    CONF_NOTIFY_ONLY_HOME,
    CONF_NOTIFY_SERVICE,
    CONF_NOTIFY_SERVICES,
    CONF_OPENGTINDB_QUERY_ID,
    CONF_QUICK_LISTS,
    CONF_TODO_ENTITY,
    CONF_UNKNOWN_MODE,
    CONF_USE_EANSUCHE,
    CONF_USE_OFFLINE_CATALOG,
    CONF_USE_OPENFOODFACTS,
    CONF_USER_AGENT,
    DEFAULT_CATALOG_BASE_URL,
    DEFAULT_OPTIONS,
    DEFAULT_TODO_ENTITY,
    DOMAIN,
    DUPLICATE_MODES,
    NAME_FORMATS,
    PANEL_URL_PATH,
    UNKNOWN_MODES,
    configured_notify_services,
    normalize_notify_services,
    normalize_todo_entities,
    split_notify_service,
)

NOTIFY_TEST_TAG = "shys_barcode_test"

SETTING_KEYS = (
    CONF_TODO_ENTITY,
    CONF_QUICK_LISTS,
    CONF_DUPLICATE_MODE,
    CONF_UNKNOWN_MODE,
    CONF_NAME_FORMAT,
    CONF_USE_OFFLINE_CATALOG,
    CONF_USE_OPENFOODFACTS,
    CONF_USE_EANSUCHE,
    CONF_OPENGTINDB_QUERY_ID,
    CONF_USER_AGENT,
    CONF_NOTIFY_SERVICES,
    CONF_NOTIFY_ONLY_HOME,
    CONF_NOTIFY_ON_SUCCESS,
    CONF_ESPHOME_DEVICE_PREFIX,
    CONF_CATALOG_BASE_URL,
)

_BOOL_KEYS = (
    CONF_USE_OFFLINE_CATALOG,
    CONF_USE_OPENFOODFACTS,
    CONF_USE_EANSUCHE,
    CONF_NOTIFY_ONLY_HOME,
    CONF_NOTIFY_ON_SUCCESS,
)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def effective_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    """Aktuelle Optionen in der gespeicherten Form, inkl. Legacy-Notify-Migration."""
    values = {**DEFAULT_OPTIONS, **(config or {})}
    return {
        CONF_TODO_ENTITY: values.get(CONF_TODO_ENTITY) or DEFAULT_TODO_ENTITY,
        CONF_QUICK_LISTS: normalize_todo_entities(values.get(CONF_QUICK_LISTS)),
        CONF_DUPLICATE_MODE: values.get(CONF_DUPLICATE_MODE) or DEFAULT_OPTIONS[CONF_DUPLICATE_MODE],
        CONF_UNKNOWN_MODE: values.get(CONF_UNKNOWN_MODE) or DEFAULT_OPTIONS[CONF_UNKNOWN_MODE],
        CONF_NAME_FORMAT: values.get(CONF_NAME_FORMAT) or DEFAULT_OPTIONS[CONF_NAME_FORMAT],
        CONF_USE_OFFLINE_CATALOG: _as_bool(values.get(CONF_USE_OFFLINE_CATALOG), True),
        CONF_USE_OPENFOODFACTS: _as_bool(values.get(CONF_USE_OPENFOODFACTS), True),
        CONF_USE_EANSUCHE: _as_bool(values.get(CONF_USE_EANSUCHE), True),
        CONF_OPENGTINDB_QUERY_ID: (values.get(CONF_OPENGTINDB_QUERY_ID) or "").strip(),
        CONF_USER_AGENT: (values.get(CONF_USER_AGENT) or "").strip(),
        CONF_NOTIFY_SERVICES: configured_notify_services(values),
        CONF_NOTIFY_ONLY_HOME: _as_bool(values.get(CONF_NOTIFY_ONLY_HOME), False),
        CONF_NOTIFY_ON_SUCCESS: _as_bool(values.get(CONF_NOTIFY_ON_SUCCESS), False),
        CONF_ESPHOME_DEVICE_PREFIX: (values.get(CONF_ESPHOME_DEVICE_PREFIX) or "").strip(),
        CONF_CATALOG_BASE_URL: (values.get(CONF_CATALOG_BASE_URL) or "").strip(),
    }


def available_todo_lists(hass: HomeAssistant | None, extra: list[str] | None = None) -> list[dict[str, str]]:
    """Alle todo.*-Listen mit Friendly-Name, inkl. bereits konfigurierter Entitäten."""
    result: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(entity_id: str, name: str | None = None) -> None:
        entity = (entity_id or "").strip()
        if not entity.startswith("todo.") or entity in seen:
            return
        seen.add(entity)
        label = (name or "").strip() or entity
        result.append({"entity_id": entity, "name": label})

    if hass is not None:
        try:
            states = hass.states.async_all("todo")
        except Exception:  # noqa: BLE001 - States sind optional
            states = []
        for state in states:
            entity_id = getattr(state, "entity_id", "") or ""
            attrs = getattr(state, "attributes", None) or {}
            add(entity_id, attrs.get("friendly_name"))
    for entity_id in extra or []:
        add(entity_id)
    result.sort(key=lambda item: item["name"].casefold())
    return result


def build_settings_payload(hass: HomeAssistant | None, config: dict[str, Any] | None) -> dict[str, Any]:
    """Payload für get_settings: aktuelle Optionen plus Auswahllisten."""
    settings = effective_settings(config)
    extras = [settings[CONF_TODO_ENTITY], *settings[CONF_QUICK_LISTS]]
    return {
        "settings": settings,
        "available_todo_lists": available_todo_lists(hass, extras),
        "available_notify_services": available_notify_services(hass, settings[CONF_NOTIFY_SERVICES]),
        "default_catalog_base_url": DEFAULT_CATALOG_BASE_URL,
    }


def validate_settings(user_input: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    """Eingaben prüfen und in speicherbare Optionen wandeln. Wirft ValueError."""
    if not isinstance(user_input, dict):
        raise TypeError("Einstellungen müssen ein Objekt sein")
    base = effective_settings(current)
    incoming = {key: user_input[key] for key in SETTING_KEYS if key in user_input}
    if CONF_NOTIFY_SERVICES not in incoming:
        incoming[CONF_NOTIFY_SERVICES] = configured_notify_services({**base, **user_input})
    merged = {**base, **incoming}
    for key in _BOOL_KEYS:
        merged[key] = _as_bool(merged.get(key), bool(base.get(key, False)))
    for key in TEXT_OPTION_KEYS:
        merged[key] = merged.get(key)
    data = normalize_options(merged)
    data.pop(CONF_NOTIFY_SERVICE, None)
    if not str(data.get(CONF_TODO_ENTITY, "")).startswith("todo."):
        raise ValueError("Bitte eine To-do-Liste (todo.*) wählen.")
    if data[CONF_DUPLICATE_MODE] not in DUPLICATE_MODES:
        raise ValueError(f"Ungültiges Duplikat-Verhalten: {data[CONF_DUPLICATE_MODE]}")
    if data[CONF_UNKNOWN_MODE] not in UNKNOWN_MODES:
        raise ValueError(f"Ungültiges Verhalten für unbekannte Codes: {data[CONF_UNKNOWN_MODE]}")
    if data[CONF_NAME_FORMAT] not in NAME_FORMATS:
        raise ValueError(f"Ungültiges Textformat: {data[CONF_NAME_FORMAT]}")
    base_url = data.get(CONF_CATALOG_BASE_URL, "")
    if base_url and not base_url.startswith(("https://", "http://")):
        raise ValueError("Die Katalog-Adresse muss mit https:// beginnen.")
    if not validate_notify_services(data.get(CONF_NOTIFY_SERVICES, [])):
        raise ValueError("Jeder Benachrichtigungsdienst muss mit notify. beginnen.")
    # Nur bekannte Options-Schlüssel speichern (kein Legacy-notify_service, kein Titel).
    return {key: data[key] for key in SETTING_KEYS}


def config_entry(hass: HomeAssistant):
    """Den einzigen Config-Entry der Integration holen."""
    try:
        entries = hass.config_entries.async_entries(DOMAIN)
    except Exception:  # noqa: BLE001
        return None
    return entries[0] if entries else None


def build_test_notification() -> dict[str, Any]:
    """Kurze Test-Push für die eingerichteten Notify-Dienste."""
    panel = f"/{PANEL_URL_PATH}"
    return {
        "title": "SHYS Barcode Scanner – Test",
        "message": "Push-Benachrichtigungen sind richtig eingerichtet.",
        "data": {"tag": NOTIFY_TEST_TAG, "url": panel, "clickAction": panel},
    }


def split_test_targets(services: list[str]) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
    """Notify-Dienste in (domain, name) zerlegen; ungültige als Fehler zurückgeben."""
    targets: list[tuple[str, str]] = []
    errors: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for service in normalize_notify_services(services):
        if not service.startswith("notify."):
            errors.append({"service": service, "error": "Jeder Dienst muss mit notify. beginnen."})
            continue
        domain, name = split_notify_service(service)
        key = (domain, name)
        if not name or key in seen:
            continue
        seen.add(key)
        targets.append(key)
    return targets, errors
