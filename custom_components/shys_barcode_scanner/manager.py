"""Scan-Ablauf: lokale DB -> API-Kaskade -> To-do-Liste -> Rückmeldung an ESPHome."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import slugify

from .const import (
    ACTION_ADDED,
    ACTION_INCREMENTED,
    ACTION_NONE,
    ACTION_SKIPPED,
    CONF_DUPLICATE_MODE,
    CONF_ESPHOME_DEVICE_PREFIX,
    CONF_NAME_FORMAT,
    CONF_NOTIFY_ONLY_HOME,
    CONF_OPENGTINDB_QUERY_ID,
    CONF_TODO_ENTITY,
    CONF_UNKNOWN_MODE,
    CONF_USE_EANSUCHE,
    CONF_USE_OPENFOODFACTS,
    CONF_USER_AGENT,
    DEFAULT_OPTIONS,
    DEFAULT_TODO_ENTITY,
    DUPLICATE_INCREMENT,
    DUPLICATE_SKIP,
    EVENT_BARCODE_SCANNED,
    EVENT_NOTIFICATION_ACTION,
    EVENT_SCAN_PROCESSED,
    NAME_FORMAT_PRODUCT_ONLY,
    NOTIFY_ACTION_DISMISS_PREFIX,
    NOTIFY_ACTION_REPLY_GENERIC,
    NOTIFY_ACTION_REPLY_PREFIX,
    NOTIFY_TAG_PREFIX,
    PANEL_TITLE,
    PANEL_URL_PATH,
    SIGNAL_UPDATE,
    SOURCE_NOTIFICATION,
    STATUS_DUPLICATE,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNKNOWN,
    UNKNOWN_ADD_EAN_AND_PENDING,
    configured_notify_services,
    split_notify_service,
)
from .lookup import EanLookup, is_valid_gtin
from .storage import BarcodeStorage, clean_code

_LOGGER = logging.getLogger(__name__)

QUANTITY_RE = re.compile(r"^\s*(\d+)\s*[xX×]\s+(.+?)\s*$")


def format_item_name(name: str, brand: str | None, name_format: str) -> str:
    """Text für die Liste. Marke nur, wenn sie nicht schon im Namen steht."""
    name = " ".join((name or "").split())
    brand = " ".join((brand or "").split())
    if name_format == NAME_FORMAT_PRODUCT_ONLY or not brand:
        return name
    if brand.casefold() in name.casefold():
        return name
    return f"{brand} {name}"


def split_quantity(summary: str) -> tuple[int, str]:
    """"3x Milch" -> (3, "Milch"), "Milch" -> (1, "Milch")."""
    match = QUANTITY_RE.match(summary or "")
    if match:
        return int(match.group(1)), match.group(2)
    return 1, (summary or "").strip()


def notification_tag(ean: str) -> str:
    """Tag der Push-Nachricht zu einem Code (zum Ersetzen/Löschen auf dem Handy)."""
    return f"{NOTIFY_TAG_PREFIX}{ean}"


def build_unknown_notification(ean: str) -> dict[str, Any]:
    """Payload für notify.mobile_app_*: Direkt-Antwort + Verwerfen + Klick öffnet das Panel."""
    panel = f"/{PANEL_URL_PATH}"
    return {
        "title": "Unbekannter Barcode",
        "message": (
            f"Code {ean} wurde nicht gefunden. Antworte direkt mit dem Produktnamen "
            f"oder öffne das Panel „{PANEL_TITLE}“."
        ),
        "data": {
            "tag": notification_tag(ean),
            "url": panel,  # iOS: Tippen auf die Nachricht
            "clickAction": panel,  # Android: Tippen auf die Nachricht
            # iOS liefert "tag" nicht zurück, dafür action_data -> EAN zusätzlich hier
            "action_data": {"shys_ean": ean},
            "actions": [
                {
                    "action": f"{NOTIFY_ACTION_REPLY_PREFIX}{ean}",
                    "title": "Produkt benennen",
                    "behavior": "textInput",
                    "textInputButtonTitle": "Speichern",
                    "textInputPlaceholder": "Produktname, z. B. Hafermilch",
                },
                {
                    "action": f"{NOTIFY_ACTION_DISMISS_PREFIX}{ean}",
                    "title": "Verwerfen",
                    "destructive": True,
                },
            ],
        },
    }


def parse_notification_action(data: dict[str, Any]) -> tuple[str, str] | None:
    """mobile_app_notification_action -> ("reply"|"dismiss", ean) oder None (nicht von uns).

    Reihenfolge:
    1. Eigene Aktions-IDs SHYS_REPLY_<ean> / SHYS_DISMISS_<ean> (iOS + Android).
    2. Generisches "REPLY" nur, wenn es eindeutig zu uns gehört: tag "shys_barcode_<ean>"
       (Android schickt alle Daten der Nachricht zurück) oder action_data.shys_ean (iOS).
    Ein REPLY ohne unsere Kennung (z. B. von anderen Automationen) wird ignoriert.
    """
    action = str(data.get("action") or "")
    if action.startswith(NOTIFY_ACTION_REPLY_PREFIX):
        ean = clean_code(action[len(NOTIFY_ACTION_REPLY_PREFIX):])
        return ("reply", ean) if ean else None
    if action.startswith(NOTIFY_ACTION_DISMISS_PREFIX):
        ean = clean_code(action[len(NOTIFY_ACTION_DISMISS_PREFIX):])
        return ("dismiss", ean) if ean else None
    if action != NOTIFY_ACTION_REPLY_GENERIC:
        return None
    tag = str(data.get("tag") or "")
    if tag:
        if not tag.startswith(NOTIFY_TAG_PREFIX):
            return None  # gehört zu einer anderen Automation
        ean = clean_code(tag[len(NOTIFY_TAG_PREFIX):])
        return ("reply", ean) if ean else None
    action_data = data.get("action_data")
    if isinstance(action_data, dict) and action_data.get("shys_ean"):
        ean = clean_code(action_data["shys_ean"])
        return ("reply", ean) if ean else None
    return None


def clean_reply_text(value: Any) -> str:
    """Antworttext säubern; leer/"null" (Android ohne Eingabe) -> ""."""
    text = " ".join(str(value or "").split())
    if text.casefold() in ("null", "none"):
        return ""
    return text[:255]


def esphome_service_name(device: str) -> str:
    """ESPHome-Gerätename -> Aktionsname, z. B. barcode-scanner -> barcode_scanner_show_result."""
    slug = re.sub(r"[^a-z0-9_]+", "_", device.strip().lower()).strip("_")
    return f"{slug}_show_result"



MOBILE_APP_PREFIX = "mobile_app_"
_UNAVAILABLE_STATES = frozenset({"", "unknown", "unavailable", "none"})


def mobile_app_slug(service_name: str) -> str | None:
    """notify-Dienstname 'mobile_app_pixel' -> 'pixel', sonst None."""
    if service_name.startswith(MOBILE_APP_PREFIX):
        slug = service_name[len(MOBILE_APP_PREFIX):]
        return slug or None
    return None


def find_device_tracker(hass: HomeAssistant, slug: str) -> str | None:
    """device_tracker zu einem mobile_app-Slug finden."""
    direct = f"device_tracker.{slug}"
    if hass.states.get(direct) is not None:
        return direct

    try:
        from homeassistant.helpers import device_registry as dr_mod
        from homeassistant.helpers import entity_registry as er_mod

        entity_reg = er_mod.async_get(hass)
        device_reg = dr_mod.async_get(hass)
    except Exception:  # noqa: BLE001 - Registries sind optional
        return None
    if entity_reg is None:
        return None

    wanted = slugify(slug)
    for entry in entity_reg.entities.values():
        entity_id = getattr(entry, "entity_id", "") or ""
        domain = getattr(entry, "domain", None) or (entity_id.split(".", 1)[0] if entity_id else "")
        if domain != "device_tracker":
            continue
        platform = getattr(entry, "platform", "") or ""
        if platform and platform != "mobile_app":
            continue
        object_id = entity_id.split(".", 1)[-1]
        if object_id == slug or slugify(object_id) == wanted:
            return entity_id
        device_id = getattr(entry, "device_id", None)
        if not device_id or device_reg is None:
            continue
        device = device_reg.async_get(device_id)
        if device is None:
            continue
        for name in (getattr(device, "name_by_user", None), getattr(device, "name", None)):
            if name and slugify(name) == wanted:
                return entity_id
    return None


def _state_is_home(state: str | None) -> bool | None:
    """True = Zuhause, False = explizit nicht Zuhause, None = unklar (fail open)."""
    if state is None:
        return None
    value = str(state).strip()
    if value == "home":
        return True
    if value.casefold() in _UNAVAILABLE_STATES:
        return None
    return False


def is_tracker_home(hass: HomeAssistant, tracker_entity_id: str) -> bool | None:
    """Anwesenheit über person.* (falls verknüpft) oder den device_tracker selbst."""
    linked: list[bool | None] = []
    try:
        persons = hass.states.async_all("person")
    except Exception:  # noqa: BLE001
        persons = []
    for person in persons:
        trackers = (getattr(person, "attributes", None) or {}).get("device_trackers") or []
        if tracker_entity_id in trackers:
            verdict = _state_is_home(getattr(person, "state", None))
            if verdict is True:
                return True
            linked.append(verdict)
    if linked:
        # Mindestens eine Person verknüpft, keine davon home.
        if all(v is None for v in linked):
            return None
        return False
    state = hass.states.get(tracker_entity_id)
    if state is None:
        return None
    return _state_is_home(state.state)


def should_send_notification(hass: HomeAssistant, service: str, only_home: bool) -> bool:
    """True, wenn der Notify-Dienst eine Unbekannt-Nachricht erhalten soll."""
    if not only_home:
        return True
    domain, name = split_notify_service(service)
    slug = mobile_app_slug(name) if domain == "notify" else None
    if not slug:
        _LOGGER.debug(
            "notify_only_home: %s ist keinem Handy zuordenbar, sende trotzdem", service
        )
        return True
    tracker = find_device_tracker(hass, slug)
    if not tracker:
        _LOGGER.debug(
            "notify_only_home: kein device_tracker für %s gefunden, sende trotzdem", service
        )
        return True
    verdict = is_tracker_home(hass, tracker)
    if verdict is True:
        return True
    if verdict is None:
        _LOGGER.debug(
            "notify_only_home: Anwesenheit von %s (%s) unklar, sende trotzdem",
            service,
            tracker,
        )
        return True
    _LOGGER.debug(
        "notify_only_home: %s (%s) ist nicht zuhause, überspringe", service, tracker
    )
    return False


class BarcodeManager:
    """Verarbeitet Scans und hält den letzten Status."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: dict[str, Any],
        storage: BarcodeStorage,
        session: Any,
    ) -> None:
        self.hass = hass
        self.storage = storage
        self._session = session
        self._config: dict[str, Any] = {}
        self.lookup: EanLookup
        self.update_config(config)
        self.last_result: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._unsub_event = None
        self._unsub_notify_action = None

    # ----------------------------------------------------------- Konfig
    def update_config(self, config: dict[str, Any]) -> None:
        self._config = {**DEFAULT_OPTIONS, **config}
        self.lookup = EanLookup(
            self._session,
            user_agent=self._config[CONF_USER_AGENT] or DEFAULT_OPTIONS[CONF_USER_AGENT],
            use_openfoodfacts=bool(self._config[CONF_USE_OPENFOODFACTS]),
            use_eansuche=bool(self._config[CONF_USE_EANSUCHE]),
            opengtindb_query_id=self._config.get(CONF_OPENGTINDB_QUERY_ID) or "",
        )

    @property
    def config(self) -> dict[str, Any]:
        return dict(self._config)

    @property
    def todo_entity(self) -> str:
        return self._config.get(CONF_TODO_ENTITY) or DEFAULT_TODO_ENTITY

    # ------------------------------------------------------------ Events
    def async_start(self) -> None:
        self._unsub_event = self.hass.bus.async_listen(EVENT_BARCODE_SCANNED, self._handle_event)
        self._unsub_notify_action = self.hass.bus.async_listen(
            EVENT_NOTIFICATION_ACTION, self._handle_notification_action
        )

    def async_stop(self) -> None:
        if self._unsub_event:
            self._unsub_event()
            self._unsub_event = None
        if self._unsub_notify_action:
            self._unsub_notify_action()
            self._unsub_notify_action = None

    @callback
    def _handle_notification_action(self, event: Event) -> None:
        data = event.data or {}
        parsed = parse_notification_action(data)
        if parsed is None:
            return
        kind, ean = parsed
        self.hass.async_create_task(
            self.async_handle_notification_action(kind, ean, data.get("reply_text")),
            "shys_barcode_scanner_notification_action",
        )

    async def async_handle_notification_action(
        self, kind: str, ean: str, reply_text: Any = None
    ) -> dict[str, Any] | None:
        """Antwort/Verwerfen aus der Push-Nachricht verarbeiten (Fehler nie nach außen)."""
        try:
            async with self._lock:
                if kind == "dismiss":
                    await self.async_dismiss(ean)
                    return {"ean": ean, "dismissed": True}
                name = clean_reply_text(reply_text)
                if not name:
                    _LOGGER.info("Leere Antwort für %s ignoriert – Code bleibt in der Anlern-Liste", ean)
                    return None
                existing = self.storage.get_product(ean)
                if existing is not None:
                    # Zweite, zeitgleiche Antwort: schon bekannt, nur aufräumen.
                    await self.storage.remove_pending(ean)
                    await self._async_clear_notification(ean)
                    self.async_notify_listeners()
                    return {"ean": ean, "already_known": True, "product": existing}
                return await self.async_learn(
                    ean, name, add_to_list=True, source=SOURCE_NOTIFICATION
                )
        except Exception:  # Push-Antworten dürfen HA nie stören
            _LOGGER.exception("Push-Aktion %s für %s fehlgeschlagen", kind, ean)
            return None

    @callback
    def _handle_event(self, event: Event) -> None:
        data = event.data or {}
        code = data.get("ean") or data.get("code") or data.get("barcode")
        if not code:
            _LOGGER.debug("Event ohne ean ignoriert: %s", data)
            return
        self.hass.async_create_task(
            self.async_handle_scan(code, device=data.get("device")),
            "shys_barcode_scanner_scan",
        )

    @callback
    def async_notify_listeners(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    # -------------------------------------------------------------- Scan
    async def async_handle_scan(self, raw_code: Any, device: str | None = None) -> dict[str, Any]:
        """Kompletter Ablauf für einen Scan. Gibt das Ergebnis als dict zurück."""
        async with self._lock:
            try:
                result = await self._process_scan(raw_code, device)
            except Exception:  # ein Scan darf HA nie stören
                _LOGGER.exception("Fehler bei der Verarbeitung von %s", raw_code)
                result = self._result(clean_code(raw_code), STATUS_ERROR, device=device)
            self.last_result = result
        await self._async_feedback(result, device)
        self.hass.bus.async_fire(EVENT_SCAN_PROCESSED, dict(result))
        self.async_notify_listeners()
        return result

    def _result(self, ean: str, status: str, **extra: Any) -> dict[str, Any]:
        base = {
            "ean": ean,
            "status": status,
            "name": "",
            "brand": "",
            "product": "",
            "source": None,
            "target_list": None,
            "action": ACTION_NONE,
            "item": None,
            "device": None,
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        base.update({k: v for k, v in extra.items() if v is not None or k not in base})
        return base

    async def _process_scan(self, raw_code: Any, device: str | None) -> dict[str, Any]:
        code = clean_code(raw_code)
        if not code:
            return self._result("", STATUS_ERROR, device=device)

        # 1) Lokale Datenbank zuerst (offline, auch eigene Codes/QR)
        product = self.storage.get_product(code)
        if product is not None:
            await self.storage.touch_product(code)
            return await self._add_product_to_list(product, device)

        # 2) API-Kaskade nur für echte EAN/GTIN
        lookup_failed = False
        if is_valid_gtin(code) and self.lookup.any_enabled:
            found = await self.lookup.async_lookup(code)
            if found.found:
                product = await self.storage.upsert_product(
                    code, found.name, brand=found.brand, source=found.source or "api"
                )
                await self.storage.touch_product(code)
                return await self._add_product_to_list(product, device)
            lookup_failed = found.all_failed

        # 3) Unbekannt -> Anlern-Liste
        return await self._handle_unknown(code, device, lookup_failed)

    async def _add_product_to_list(self, product: dict[str, Any], device: str | None) -> dict[str, Any]:
        item_name = format_item_name(
            product.get("name", ""), product.get("brand"), self._config[CONF_NAME_FORMAT]
        )
        target = product.get("target_list") or self.todo_entity
        try:
            action, item = await self._async_add_to_todo(target, item_name)
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.error("Konnte '%s' nicht auf %s setzen: %s", item_name, target, err)
            return self._result(
                product["ean"], STATUS_ERROR, name=item_name, brand=product.get("brand") or "",
                product=product.get("name") or "", source=product.get("source"),
                target_list=target, device=device,
            )
        status = STATUS_DUPLICATE if action == ACTION_SKIPPED else STATUS_OK
        return self._result(
            product["ean"], status, name=item_name, brand=product.get("brand") or "",
            product=product.get("name") or "", source=product.get("source"),
            target_list=target, action=action, item=item, device=device,
        )

    async def _handle_unknown(self, code: str, device: str | None, lookup_failed: bool) -> dict[str, Any]:
        _entry, is_new = await self.storage.add_pending(code, device)
        action = ACTION_NONE
        target = None
        item = None
        if self._config[CONF_UNKNOWN_MODE] == UNKNOWN_ADD_EAN_AND_PENDING:
            target = self.todo_entity
            try:
                action, item = await self._async_add_to_todo(target, code)
            except (HomeAssistantError, ValueError) as err:
                _LOGGER.error("Konnte Code %s nicht auf %s setzen: %s", code, target, err)
        if is_new:
            await self._async_notify_unknown(code)
        status = STATUS_ERROR if lookup_failed else STATUS_UNKNOWN
        return self._result(
            code, status, name=code, target_list=target, action=action, item=item, device=device,
            source=None,
        )

    # ------------------------------------------------------------ To-do
    async def _async_add_to_todo(self, entity_id: str, name: str) -> tuple[str, str]:
        """Eintrag auf die Liste setzen, Duplikat-Modus beachten. -> (aktion, text)."""
        if not entity_id or not entity_id.startswith("todo."):
            raise ValueError(f"Ungültige To-do-Liste: {entity_id!r}")
        mode = self._config[CONF_DUPLICATE_MODE]
        if mode in (DUPLICATE_SKIP, DUPLICATE_INCREMENT):
            existing = await self._async_find_existing(entity_id, name)
            if existing is not None:
                if mode == DUPLICATE_SKIP:
                    return ACTION_SKIPPED, existing["summary"]
                qty, _base = split_quantity(existing["summary"])
                new_summary = f"{qty + 1}x {name}"
                try:
                    await self.hass.services.async_call(
                        "todo",
                        "update_item",
                        {"item": existing.get("uid") or existing["summary"], "rename": new_summary},
                        target={"entity_id": entity_id},
                        blocking=True,
                    )
                    return ACTION_INCREMENTED, new_summary
                except HomeAssistantError as err:
                    # z. B. Picnic-Warenkorb kann nicht umbenennen -> einfach erneut hinzufügen
                    _LOGGER.debug("update_item auf %s nicht möglich (%s), füge erneut hinzu", entity_id, err)
        await self.hass.services.async_call(
            "todo", "add_item", {"item": name}, target={"entity_id": entity_id}, blocking=True
        )
        return ACTION_ADDED, name

    async def _async_find_existing(self, entity_id: str, name: str) -> dict[str, Any] | None:
        try:
            response = await self.hass.services.async_call(
                "todo",
                "get_items",
                {"status": ["needs_action"]},
                target={"entity_id": entity_id},
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError as err:
            _LOGGER.debug("get_items auf %s nicht möglich: %s", entity_id, err)
            return None
        items = ((response or {}).get(entity_id) or {}).get("items") or []
        wanted = name.casefold().strip()
        for item in items:
            summary = item.get("summary") or ""
            _qty, base = split_quantity(summary)
            if base.casefold() == wanted or summary.casefold().strip() == wanted:
                return item
        return None

    # ---------------------------------------------------- Benachrichtigung
    def _notify_services(self) -> list[str]:
        return configured_notify_services(self._config)

    def _notify_targets(self, *, only_home: bool | None = None) -> list[tuple[str, str]]:
        """Konfigurierte Notify-Ziele, optional nach Anwesenheit gefiltert."""
        if only_home is None:
            only_home = bool(self._config.get(CONF_NOTIFY_ONLY_HOME))
        targets: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for service in self._notify_services():
            if only_home and not should_send_notification(self.hass, service, True):
                continue
            domain, name = split_notify_service(service)
            key = (domain, name)
            if not name or key in seen:
                continue
            seen.add(key)
            targets.append(key)
        return targets

    async def _async_notify_unknown(self, code: str) -> None:
        payload = build_unknown_notification(code)
        for domain, name in self._notify_targets():
            try:
                await self.hass.services.async_call(domain, name, payload, blocking=True)
            except Exception as err:  # noqa: BLE001 - ein Handy darf die anderen nicht blockieren
                _LOGGER.warning("Benachrichtigung über %s.%s fehlgeschlagen: %s", domain, name, err)

    async def _async_clear_notification(self, code: str) -> None:
        """Push-Nachricht auf allen konfigurierten Handys entfernen (best effort)."""
        payload = {"message": "clear_notification", "data": {"tag": notification_tag(code)}}
        # Ohne Anwesenheitsfilter: auch abwesende Handys sollen die Meldung verlieren.
        for domain, name in self._notify_targets(only_home=False):
            try:
                await self.hass.services.async_call(domain, name, payload, blocking=True)
            except Exception as err:  # noqa: BLE001 - nur Aufräumen
                _LOGGER.debug(
                    "clear_notification über %s.%s für %s fehlgeschlagen: %s",
                    domain,
                    name,
                    code,
                    err,
                )

    # ----------------------------------------------- Rückmeldung an ESP
    async def _async_feedback(self, result: dict[str, Any], device: str | None) -> None:
        device = device or (self._config.get(CONF_ESPHOME_DEVICE_PREFIX) or "").strip()
        if not device:
            return
        service = esphome_service_name(device)
        if not self.hass.services.has_service("esphome", service):
            _LOGGER.debug("ESPHome-Aktion esphome.%s nicht vorhanden", service)
            return
        try:
            await self.hass.services.async_call(
                "esphome",
                service,
                {
                    "status": result["status"],
                    "ean": result["ean"],
                    "brand": result.get("brand") or "",
                    "product": result.get("product") or "",
                },
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001 - Rückmeldung ist best effort
            _LOGGER.debug("Rückmeldung an esphome.%s fehlgeschlagen: %s", service, err)

    # ---------------------------------------------------- Lernen / Pflege
    async def async_learn(
        self,
        code: str,
        name: str,
        brand: str | None = None,
        target_list: str | None = None,
        add_to_list: bool = False,
        source: str = "manual",
    ) -> dict[str, Any]:
        product = await self.storage.upsert_product(code, name, brand=brand, source=source, target_list=target_list)
        result: dict[str, Any] = {"product": product}
        if add_to_list:
            result["scan"] = await self._add_product_to_list(product, None)
        await self._async_clear_notification(clean_code(code))
        self.async_notify_listeners()
        return result

    async def async_delete(self, code: str) -> bool:
        removed = await self.storage.delete_product(code)
        self.async_notify_listeners()
        return removed

    async def async_dismiss(self, code: str) -> bool:
        removed = await self.storage.remove_pending(code)
        await self._async_clear_notification(clean_code(code))
        self.async_notify_listeners()
        return removed

    async def async_import(self, data: dict[str, Any], merge: bool = True) -> dict[str, int]:
        stats = await self.storage.import_dict(data, merge=merge)
        self.async_notify_listeners()
        return stats

    def get_state(self) -> dict[str, Any]:
        return {
            "products": self.storage.list_products(),
            "pending": self.storage.list_pending(),
            "last_result": self.last_result,
            "todo_entity": self.todo_entity,
            "config": {k: v for k, v in self._config.items() if k != CONF_OPENGTINDB_QUERY_ID},
        }
