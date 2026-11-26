"""Einrichtung über die UI: Schritt 1 nur Liste wählen, alles Weitere in den Optionen."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_DUPLICATE_MODE,
    CONF_ESPHOME_DEVICE_PREFIX,
    CONF_NAME,
    CONF_NAME_FORMAT,
    CONF_NOTIFY_SERVICE,
    CONF_OPENGTINDB_QUERY_ID,
    CONF_TODO_ENTITY,
    CONF_UNKNOWN_MODE,
    CONF_USE_EANSUCHE,
    CONF_USE_OPENFOODFACTS,
    CONF_USER_AGENT,
    DEFAULT_NAME,
    DEFAULT_OPTIONS,
    DEFAULT_TODO_ENTITY,
    DOMAIN,
    DUPLICATE_MODES,
    NAME_FORMATS,
    UNKNOWN_MODES,
)

TODO_SELECTOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="todo"))
TEXT_SELECTOR = selector.TextSelector()

TEXT_OPTION_KEYS = (
    CONF_NOTIFY_SERVICE,
    CONF_OPENGTINDB_QUERY_ID,
    CONF_USER_AGENT,
    CONF_ESPHOME_DEVICE_PREFIX,
)


def _select(options: list[str], key: str) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options, translation_key=key, mode=selector.SelectSelectorMode.DROPDOWN
        )
    )


def build_options_schema(current: dict[str, Any]) -> vol.Schema:
    """Options-Schema mit aktuellen Werten als Vorbelegung."""
    values = {**DEFAULT_OPTIONS, **current}
    schema: dict[Any, Any] = {
        vol.Required(
            CONF_TODO_ENTITY, default=values.get(CONF_TODO_ENTITY) or DEFAULT_TODO_ENTITY
        ): TODO_SELECTOR,
        vol.Required(CONF_DUPLICATE_MODE, default=values[CONF_DUPLICATE_MODE]): _select(
            DUPLICATE_MODES, CONF_DUPLICATE_MODE
        ),
        vol.Required(CONF_UNKNOWN_MODE, default=values[CONF_UNKNOWN_MODE]): _select(
            UNKNOWN_MODES, CONF_UNKNOWN_MODE
        ),
        vol.Required(CONF_NAME_FORMAT, default=values[CONF_NAME_FORMAT]): _select(
            NAME_FORMATS, CONF_NAME_FORMAT
        ),
        vol.Required(
            CONF_USE_OPENFOODFACTS, default=bool(values[CONF_USE_OPENFOODFACTS])
        ): selector.BooleanSelector(),
        vol.Required(CONF_USE_EANSUCHE, default=bool(values[CONF_USE_EANSUCHE])): selector.BooleanSelector(),
    }
    # Optionale Texte mit suggested_value, damit man sie auch wieder leeren kann
    for key in TEXT_OPTION_KEYS:
        schema[
            vol.Optional(key, description={"suggested_value": values.get(key) or ""})
        ] = TEXT_SELECTOR
    return vol.Schema(schema)


def normalize_options(user_input: dict[str, Any]) -> dict[str, Any]:
    """Leere Textfelder als "" speichern, Leerraum entfernen."""
    data = dict(user_input)
    for key in TEXT_OPTION_KEYS:
        data[key] = (data.get(key) or "").strip()
    return data


class ShysBarcodeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Einrichtung in 10 Sekunden: Liste wählen, fertig."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        if user_input is not None:
            todo = user_input.get(CONF_TODO_ENTITY, "")
            if not str(todo).startswith("todo."):
                errors[CONF_TODO_ENTITY] = "invalid_todo"
            else:
                return self.async_create_entry(
                    title=(user_input.get(CONF_NAME) or DEFAULT_NAME).strip() or DEFAULT_NAME,
                    data={CONF_TODO_ENTITY: todo},
                    options={k: v for k, v in DEFAULT_OPTIONS.items()},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_TODO_ENTITY, default=DEFAULT_TODO_ENTITY): TODO_SELECTOR,
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): TEXT_SELECTOR,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ShysBarcodeOptionsFlow()


class ShysBarcodeOptionsFlow(OptionsFlow):
    """Alle Extras: Duplikate, unbekannte Codes, Quellen, Benachrichtigung."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = {**self.config_entry.data, **self.config_entry.options}
        errors: dict[str, str] = {}
        if user_input is not None:
            data = normalize_options(user_input)
            if not str(data.get(CONF_TODO_ENTITY, "")).startswith("todo."):
                errors[CONF_TODO_ENTITY] = "invalid_todo"
            notify = data.get(CONF_NOTIFY_SERVICE, "")
            if notify and not notify.startswith("notify."):
                errors[CONF_NOTIFY_SERVICE] = "invalid_notify"
            if not errors:
                return self.async_create_entry(title="", data=data)
            current = {**current, **user_input}
        return self.async_show_form(
            step_id="init", data_schema=build_options_schema(current), errors=errors
        )
