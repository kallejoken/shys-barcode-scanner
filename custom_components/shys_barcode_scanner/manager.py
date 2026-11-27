"""Scan-Ablauf: lokale DB -> Offline-Katalog -> API-Kaskade -> To-do-Liste -> Rückmeldung an ESPHome."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
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
    CONF_CATALOG_BASE_URL,
    CONF_DUPLICATE_MODE,
    CONF_ESPHOME_DEVICE_PREFIX,
    CONF_NAME_FORMAT,
    CONF_NOTIFY_ON_SUCCESS,
    CONF_NOTIFY_ONLY_HOME,
    CONF_OPENGTINDB_QUERY_ID,
    CONF_QUICK_LISTS,
    CONF_TODO_ENTITY,
    CONF_UNKNOWN_MODE,
    CONF_USE_EANSUCHE,
    CONF_USE_OFFLINE_CATALOG,
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
    NOTIFY_ACTION_UNDO_PREFIX,
    NOTIFY_OK_TAG_PREFIX,
    NOTIFY_TAG_PREFIX,
    PANEL_TITLE,
    PANEL_URL_PATH,
    SIGNAL_UPDATE,
    SOURCE_NOTIFICATION,
    SOURCE_OFFLINE_CATALOG,
    STATUS_DUPLICATE,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNKNOWN,
    UNDO_HISTORY_LIMIT,
    UNKNOWN_ADD_EAN_AND_PENDING,
    configured_notify_services,
    normalize_notify_services,
    normalize_todo_entities,
    split_notify_service,
)
from .lookup import EanLookup, is_valid_gtin
from .storage import BarcodeStorage, clean_code

_LOGGER = logging.getLogger(__name__)

QUANTITY_RE = re.compile(r"^\s*(\d+)\s*[xX×]\s+(.+?)\s*$")
REPLY_LIST_ACTION_RE = re.compile(r"^SHYS_REPLY_L(\d+)_(.+)$")
LIST_AT_RE = re.compile(r"^(?P<name>.+?)\s+@(?P<hint>.+)$")


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


def success_notification_tag(undo_id: str) -> str:
    """Tag der optionalen Erfolgs-Push mit Rückgängig-Button."""
    return f"{NOTIFY_OK_TAG_PREFIX}{undo_id}"


def _reply_action_payload(action: str, title: str, ean: str, target_list: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
        "title": title,
        "behavior": "textInput",
        "textInputButtonTitle": "Speichern",
        "textInputPlaceholder": "Produktname, z. B. Hafermilch",
    }
    action_data: dict[str, Any] = {"shys_ean": ean}
    if target_list:
        action_data["shys_target_list"] = target_list
    payload["action_data"] = action_data
    return payload


def build_unknown_notification(
    ean: str, list_choices: list[tuple[str, str]] | None = None
) -> dict[str, Any]:
    """Payload für notify.mobile_app_*: Direkt-Antwort + Verwerfen + Klick öffnet das Panel.

    list_choices: optionale [(entity_id, Button-Titel), ...] – bei mindestens zwei
    Einträgen erscheinen zwei Listen-Buttons plus Verwerfen (Android-Maximum: 3).
    """
    panel = f"/{PANEL_URL_PATH}"
    action_data: dict[str, Any] = {"shys_ean": ean}
    if list_choices and len(list_choices) >= 2:
        actions = [
            _reply_action_payload(
                f"{NOTIFY_ACTION_REPLY_PREFIX}L{index}_{ean}",
                title,
                ean,
                entity_id,
            )
            for index, (entity_id, title) in enumerate(list_choices[:2])
        ]
        action_data["shys_target_list"] = list_choices[0][0]
    else:
        actions = [
            _reply_action_payload(f"{NOTIFY_ACTION_REPLY_PREFIX}{ean}", "Produkt benennen", ean)
        ]
    actions.append(
        {
            "action": f"{NOTIFY_ACTION_DISMISS_PREFIX}{ean}",
            "title": "Verwerfen",
            "destructive": True,
        }
    )
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
            "action_data": action_data,
            "actions": actions,
        },
    }


def build_success_notification(undo_id: str, product_name: str, list_name: str) -> dict[str, Any]:
    """Kurze Erfolgs-Push mit genau einem Rückgängig-Button."""
    panel = f"/{PANEL_URL_PATH}"
    return {
        "title": "Auf der Einkaufsliste",
        "message": f"{product_name} wurde zu {list_name} hinzugefügt.",
        "data": {
            "tag": success_notification_tag(undo_id),
            "url": panel,
            "clickAction": panel,
            "action_data": {"shys_undo_id": undo_id},
            "actions": [
                {
                    "action": f"{NOTIFY_ACTION_UNDO_PREFIX}{undo_id}",
                    "title": "Rückgängig",
                }
            ],
        },
    }


def _action_data(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get("action_data")
    return raw if isinstance(raw, dict) else {}


def parse_notification_action(data: dict[str, Any]) -> dict[str, Any] | None:
    """mobile_app_notification_action -> dict oder None (nicht von uns).

    Reihenfolge:
    1. SHYS_UNDO_<undo_id>
    2. SHYS_REPLY_L<index>_<ean> (Listenwahl in der Unbekannt-Push)
    3. SHYS_REPLY_<ean> / SHYS_DISMISS_<ean>
    4. Generisches "REPLY" nur mit tag "shys_barcode_<ean>" oder action_data.shys_ean.
    """
    action = str(data.get("action") or "")
    extra = _action_data(data)
    if action.startswith(NOTIFY_ACTION_UNDO_PREFIX):
        undo_id = action[len(NOTIFY_ACTION_UNDO_PREFIX):].strip() or str(extra.get("shys_undo_id") or "").strip()
        return {"kind": "undo", "undo_id": undo_id} if undo_id else None
    list_match = REPLY_LIST_ACTION_RE.match(action)
    if list_match:
        ean = clean_code(list_match.group(2))
        if not ean:
            return None
        parsed: dict[str, Any] = {"kind": "reply", "ean": ean, "list_index": int(list_match.group(1))}
        target = str(extra.get("shys_target_list") or "").strip()
        if target.startswith("todo."):
            parsed["target_list"] = target
        return parsed
    if action.startswith(NOTIFY_ACTION_REPLY_PREFIX):
        ean = clean_code(action[len(NOTIFY_ACTION_REPLY_PREFIX):])
        if not ean:
            return None
        parsed = {"kind": "reply", "ean": ean}
        target = str(extra.get("shys_target_list") or "").strip()
        if target.startswith("todo."):
            parsed["target_list"] = target
        return parsed
    if action.startswith(NOTIFY_ACTION_DISMISS_PREFIX):
        ean = clean_code(action[len(NOTIFY_ACTION_DISMISS_PREFIX):])
        return {"kind": "dismiss", "ean": ean} if ean else None
    if action != NOTIFY_ACTION_REPLY_GENERIC:
        return None
    tag = str(data.get("tag") or "")
    if tag:
        if not tag.startswith(NOTIFY_TAG_PREFIX) or tag.startswith(NOTIFY_OK_TAG_PREFIX):
            return None  # gehört zu einer anderen Automation bzw. Erfolgs-Push
        ean = clean_code(tag[len(NOTIFY_TAG_PREFIX):])
        return {"kind": "reply", "ean": ean} if ean else None
    if extra.get("shys_ean"):
        ean = clean_code(extra["shys_ean"])
        return {"kind": "reply", "ean": ean} if ean else None
    return None


def clean_reply_text(value: Any) -> str:
    """Antworttext säubern; leer/"null" (Android ohne Eingabe) -> ""."""
    text = " ".join(str(value or "").split())
    if text.casefold() in ("null", "none"):
        return ""
    return text[:255]


def split_reply_list_suffix(text: str) -> tuple[str, str | None]:
    """"Spülmittel @Drogerie" -> ("Spülmittel", "Drogerie"). Ohne Suffix: (text, None)."""
    match = LIST_AT_RE.fullmatch((text or "").strip())
    if not match:
        return text, None
    name = " ".join(match.group("name").split())
    hint = " ".join(match.group("hint").split())
    if not name or not hint:
        return text, None
    return name, hint


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
        catalog: Any = None,
    ) -> None:
        self.hass = hass
        self.storage = storage
        self._session = session
        self.catalog = catalog
        self._config: dict[str, Any] = {}
        self.lookup: EanLookup
        self.update_config(config)
        self.last_result: dict[str, Any] | None = None
        self._undo_log: list[dict[str, Any]] = []
        self._pending_list_choices: dict[str, list[str]] = {}
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
        if self.catalog is not None and hasattr(self.catalog, "set_base_url"):
            self.catalog.set_base_url(self._config.get(CONF_CATALOG_BASE_URL) or None)

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
        self.hass.async_create_task(
            self.async_handle_notification_action(
                parsed["kind"],
                parsed.get("ean") or "",
                data.get("reply_text"),
                target_list=parsed.get("target_list"),
                list_index=parsed.get("list_index"),
                undo_id=parsed.get("undo_id"),
            ),
            "shys_barcode_scanner_notification_action",
        )

    async def async_handle_notification_action(
        self,
        kind: str,
        ean: str,
        reply_text: Any = None,
        *,
        target_list: str | None = None,
        list_index: int | None = None,
        undo_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Antwort/Verwerfen/Undo aus der Push-Nachricht verarbeiten (Fehler nie nach außen)."""
        try:
            async with self._lock:
                if kind == "undo":
                    return await self._async_undo_scan_locked(undo_id)
                if kind == "dismiss":
                    await self.async_dismiss(ean)
                    return {"ean": ean, "dismissed": True}
                name = clean_reply_text(reply_text)
                if not name:
                    _LOGGER.info("Leere Antwort für %s ignoriert – Code bleibt in der Anlern-Liste", ean)
                    return None
                bare, hint = split_reply_list_suffix(name)
                chosen = None
                if hint:
                    chosen = self.resolve_todo_list(hint)
                    if chosen:
                        name = bare
                if chosen is None:
                    chosen = self._clean_todo_entity(target_list) or self._choice_for_index(ean, list_index)
                existing = self.storage.get_product(ean)
                if existing is not None:
                    # Zweite, zeitgleiche Antwort: schon bekannt, nur aufräumen.
                    await self.storage.remove_pending(ean)
                    await self._async_clear_notification(ean)
                    self._pending_list_choices.pop(ean, None)
                    self.async_notify_listeners()
                    return {"ean": ean, "already_known": True, "product": existing}
                result = await self.async_learn(
                    ean, name, add_to_list=True, source=SOURCE_NOTIFICATION, target_list=chosen
                )
                self._pending_list_choices.pop(ean, None)
                return result
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

        # 2) Lokaler Offline-Katalog (SQLite, < 1 ms, ohne Internet). Nur der gescannte
        #    Artikel wird in die persönliche DB übernommen, damit man ihn dort anpassen kann.
        offline = await self._async_offline_lookup(code)
        if offline is not None:
            product = await self.storage.upsert_product(
                code, offline["name"], brand=offline.get("brand") or None, source=SOURCE_OFFLINE_CATALOG
            )
            await self.storage.touch_product(code)
            return await self._add_product_to_list(product, device)

        # 3) Online-API-Kaskade nur für echte EAN/GTIN
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

        # 4) Unbekannt -> Anlern-Liste
        return await self._handle_unknown(code, device, lookup_failed)

    @property
    def offline_catalog_enabled(self) -> bool:
        return self.catalog is not None and bool(self._config.get(CONF_USE_OFFLINE_CATALOG, True))

    async def _async_offline_lookup(self, code: str) -> dict[str, Any] | None:
        if not self.offline_catalog_enabled or not code.isdigit() or not is_valid_gtin(code):
            return None
        try:
            hit = await self.catalog.async_lookup(code)
        except Exception:  # ein Katalogfehler darf den Scan nie verhindern
            _LOGGER.exception("Offline-Katalog: Lookup für %s fehlgeschlagen", code)
            return None
        if not hit or not hit.get("name"):
            return None
        return hit

    async def async_catalog_add(
        self, code: str, *, add_to_list: bool = False, target_list: str | None = None
    ) -> dict[str, Any]:
        """Artikel aus dem Offline-Katalog in die eigene DB übernehmen (optional gleich auf die Liste)."""
        if self.catalog is None:
            raise ValueError("Offline-Katalog ist nicht verfügbar")
        code = clean_code(code)
        hit = await self.catalog.async_lookup(code)
        if not hit:
            raise ValueError(f"{code} ist nicht im Offline-Katalog")
        existing = self.storage.get_product(code)
        if existing is not None and existing.get("source") != SOURCE_OFFLINE_CATALOG:
            # Eigene/gelernte Namen haben immer Vorrang und werden nicht überschrieben.
            product = existing
            if target_list:
                product = await self.storage.upsert_product(
                    code, existing["name"], brand=existing.get("brand"), source=existing.get("source"),
                    target_list=target_list,
                )
        else:
            product = await self.storage.upsert_product(
                code, hit["name"], brand=hit.get("brand") or "", source=SOURCE_OFFLINE_CATALOG,
                target_list=target_list or (existing or {}).get("target_list"),
            )
        result: dict[str, Any] = {"product": product}
        if add_to_list:
            async with self._lock:
                result["scan"] = await self._add_product_to_list(product, None)
        await self._async_clear_notification(code)
        self.async_notify_listeners()
        return result

    async def _add_product_to_list(self, product: dict[str, Any], device: str | None) -> dict[str, Any]:
        item_name = format_item_name(
            product.get("name", ""), product.get("brand"), self._config[CONF_NAME_FORMAT]
        )
        target = product.get("target_list") or self.todo_entity
        try:
            action, item, previous = await self._async_add_to_todo(target, item_name)
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.error("Konnte '%s' nicht auf %s setzen: %s", item_name, target, err)
            return self._result(
                product["ean"], STATUS_ERROR, name=item_name, brand=product.get("brand") or "",
                product=product.get("name") or "", source=product.get("source"),
                target_list=target, device=device,
            )
        status = STATUS_DUPLICATE if action == ACTION_SKIPPED else STATUS_OK
        result = self._result(
            product["ean"], status, name=item_name, brand=product.get("brand") or "",
            product=product.get("name") or "", source=product.get("source"),
            target_list=target, action=action, item=item, device=device,
        )
        if action in (ACTION_ADDED, ACTION_INCREMENTED):
            undo_id = self._register_undo(
                ean=product["ean"],
                product_name=item_name,
                todo_entity=target,
                added_item_summary=item,
                action_kind=action,
                previous_summary=previous,
            )
            result["undo_id"] = undo_id
            await self._async_notify_success(undo_id, item_name, target)
        return result

    async def _handle_unknown(self, code: str, device: str | None, lookup_failed: bool) -> dict[str, Any]:
        _entry, is_new = await self.storage.add_pending(code, device)
        action = ACTION_NONE
        target = None
        item = None
        previous = None
        if self._config[CONF_UNKNOWN_MODE] == UNKNOWN_ADD_EAN_AND_PENDING:
            target = self.todo_entity
            try:
                action, item, previous = await self._async_add_to_todo(target, code)
            except (HomeAssistantError, ValueError) as err:
                _LOGGER.error("Konnte Code %s nicht auf %s setzen: %s", code, target, err)
        if is_new:
            await self._async_notify_unknown(code)
        result = self._result(
            code,
            STATUS_ERROR if lookup_failed else STATUS_UNKNOWN,
            name=code, target_list=target, action=action, item=item, device=device,
            source=None,
        )
        if action in (ACTION_ADDED, ACTION_INCREMENTED):
            result["undo_id"] = self._register_undo(
                ean=code,
                product_name=code,
                todo_entity=target or self.todo_entity,
                added_item_summary=item or code,
                action_kind=action,
                previous_summary=previous,
            )
        return result

    # ------------------------------------------------------------ To-do
    async def _async_add_to_todo(self, entity_id: str, name: str) -> tuple[str, str, str | None]:
        """Eintrag auf die Liste setzen, Duplikat-Modus beachten. -> (aktion, text, vorheriger Text)."""
        if not entity_id or not entity_id.startswith("todo."):
            raise ValueError(f"Ungültige To-do-Liste: {entity_id!r}")
        mode = self._config[CONF_DUPLICATE_MODE]
        if mode in (DUPLICATE_SKIP, DUPLICATE_INCREMENT):
            existing = await self._async_find_existing(entity_id, name)
            if existing is not None:
                if mode == DUPLICATE_SKIP:
                    return ACTION_SKIPPED, existing["summary"], None
                previous = existing["summary"]
                qty, _base = split_quantity(previous)
                new_summary = f"{qty + 1}x {name}"
                try:
                    await self.hass.services.async_call(
                        "todo",
                        "update_item",
                        {"item": existing.get("uid") or previous, "rename": new_summary},
                        target={"entity_id": entity_id},
                        blocking=True,
                    )
                    return ACTION_INCREMENTED, new_summary, previous
                except HomeAssistantError as err:
                    # z. B. Picnic-Warenkorb kann nicht umbenennen -> einfach erneut hinzufügen
                    _LOGGER.debug("update_item auf %s nicht möglich (%s), füge erneut hinzu", entity_id, err)
        await self.hass.services.async_call(
            "todo", "add_item", {"item": name}, target={"entity_id": entity_id}, blocking=True
        )
        return ACTION_ADDED, name, None

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

    async def async_test_push(self, notify_services: list[str] | None = None) -> dict[str, Any]:
        """Test-Push an die angegebenen (sonst gespeicherten) Dienste, ohne Anwesenheitsfilter."""
        from .settings import build_test_notification, split_test_targets

        if notify_services is None:
            services = self._notify_services()
        else:
            services = normalize_notify_services(notify_services)
        if not services:
            return {"ok": False, "sent": [], "errors": [], "reason": "no_services"}
        targets, errors = split_test_targets(services)
        payload = build_test_notification()
        sent: list[str] = []
        for domain, name in targets:
            full = f"{domain}.{name}"
            try:
                await self.hass.services.async_call(domain, name, payload, blocking=True)
                sent.append(full)
            except Exception as err:  # noqa: BLE001 - Testdienst darf die anderen nicht blockieren
                errors.append({"service": full, "error": str(err)})
        return {"ok": bool(sent) and not errors, "sent": sent, "errors": errors}

    def todo_display_name(self, entity_id: str) -> str:
        """Lesbarer Listenname (friendly_name, sonst Objekt-ID)."""
        state = self.hass.states.get(entity_id)
        if state is not None:
            name = (getattr(state, "attributes", None) or {}).get("friendly_name")
            if name:
                return str(name)
        object_id = entity_id.split(".", 1)[-1] if entity_id else ""
        return object_id.replace("_", " ") or entity_id

    def list_action_title(self, entity_id: str, max_len: int = 22) -> str:
        """Kurzer Button-Text für die Unbekannt-Push, z. B. '→ Einkaufsliste'."""
        name = self.todo_display_name(entity_id)
        if len(name) > max_len:
            name = name[: max_len - 1] + "…"
        return f"→ {name}"

    def _unknown_notify_lists(self) -> list[tuple[str, str]]:
        """Listen-Buttons für die Unbekannt-Push: Standard plus erste abweichende Schnell-Liste."""
        default = self.todo_entity
        extras = [
            entity
            for entity in normalize_todo_entities(self._config.get(CONF_QUICK_LISTS))
            if entity != default
        ]
        entities = [default]
        if extras:
            entities.append(extras[0])
        return [(entity, self.list_action_title(entity)) for entity in entities if entity]

    @staticmethod
    def _clean_todo_entity(entity_id: str | None) -> str | None:
        entity = (entity_id or "").strip()
        return entity if entity.startswith("todo.") else None

    def _choice_for_index(self, ean: str, list_index: int | None) -> str | None:
        if list_index is None:
            return None
        choices = self._pending_list_choices.get(ean) or []
        if 0 <= list_index < len(choices):
            return choices[list_index]
        return None

    def resolve_todo_list(self, hint: str) -> str | None:
        """todo-Liste anhand Name, Entity-ID oder Objekt-ID finden."""
        hint = " ".join((hint or "").split())
        if not hint:
            return None
        candidates: list[str] = []
        seen: set[str] = set()

        def add(entity_id: str | None) -> None:
            entity = (entity_id or "").strip()
            if entity.startswith("todo.") and entity not in seen:
                seen.add(entity)
                candidates.append(entity)

        add(self.todo_entity)
        for entity in normalize_todo_entities(self._config.get(CONF_QUICK_LISTS)):
            add(entity)
        try:
            for state in self.hass.states.async_all("todo"):
                add(getattr(state, "entity_id", None))
        except Exception as err:  # noqa: BLE001 - States sind optional
            _LOGGER.debug("todo-Listen konnten nicht gelesen werden: %s", err)

        hint_cf = hint.casefold()
        for entity in candidates:
            if entity.casefold() == hint_cf:
                return entity
        if hint.startswith("todo.") and len(hint) > 5:
            return hint
        for entity in candidates:
            if entity.split(".", 1)[-1].casefold() == hint_cf:
                return entity
        for entity in candidates:
            if self.todo_display_name(entity).casefold() == hint_cf:
                return entity
        wanted = slugify(hint)
        if wanted:
            for entity in candidates:
                if slugify(self.todo_display_name(entity)) == wanted:
                    return entity
                if slugify(entity.split(".", 1)[-1]) == wanted:
                    return entity
        return None

    async def _async_notify_unknown(self, code: str) -> None:
        choices = self._unknown_notify_lists()
        if len(choices) >= 2:
            self._pending_list_choices[code] = [entity for entity, _title in choices]
            payload = build_unknown_notification(code, list_choices=choices)
        else:
            self._pending_list_choices.pop(code, None)
            payload = build_unknown_notification(code)
        for domain, name in self._notify_targets():
            try:
                await self.hass.services.async_call(domain, name, payload, blocking=True)
            except Exception as err:  # noqa: BLE001 - ein Handy darf die anderen nicht blockieren
                _LOGGER.warning("Benachrichtigung über %s.%s fehlgeschlagen: %s", domain, name, err)

    async def _async_notify_success(self, undo_id: str, product_name: str, todo_entity: str) -> None:
        if not self._config.get(CONF_NOTIFY_ON_SUCCESS):
            return
        if not self._notify_services():
            return
        payload = build_success_notification(
            undo_id, product_name, self.todo_display_name(todo_entity) or todo_entity
        )
        for domain, name in self._notify_targets():
            try:
                await self.hass.services.async_call(domain, name, payload, blocking=True)
            except Exception as err:  # noqa: BLE001 - ein Handy darf die anderen nicht blockieren
                _LOGGER.warning("Erfolgs-Push über %s.%s fehlgeschlagen: %s", domain, name, err)

    async def _async_clear_notification_tag(self, tag: str) -> None:
        """Push-Nachricht mit dem angegebenen Tag auf allen Handys entfernen (best effort)."""
        payload = {"message": "clear_notification", "data": {"tag": tag}}
        # Ohne Anwesenheitsfilter: auch abwesende Handys sollen die Meldung verlieren.
        for domain, name in self._notify_targets(only_home=False):
            try:
                await self.hass.services.async_call(domain, name, payload, blocking=True)
            except Exception as err:  # noqa: BLE001 - nur Aufräumen
                _LOGGER.debug(
                    "clear_notification über %s.%s für %s fehlgeschlagen: %s",
                    domain,
                    name,
                    tag,
                    err,
                )

    async def _async_clear_notification(self, code: str) -> None:
        """Unbekannt-Push auf allen konfigurierten Handys entfernen (best effort)."""
        await self._async_clear_notification_tag(notification_tag(code))

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

    def _register_undo(
        self,
        *,
        ean: str,
        product_name: str,
        todo_entity: str,
        added_item_summary: str,
        action_kind: str,
        previous_summary: str | None = None,
    ) -> str:
        undo_id = uuid.uuid4().hex[:16]
        self._undo_log.append(
            {
                "undo_id": undo_id,
                "ean": ean,
                "product_name": product_name,
                "todo_entity": todo_entity,
                "added_item_summary": added_item_summary,
                "action_kind": action_kind,
                "previous_summary": previous_summary,
                "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                "undone": False,
            }
        )
        if len(self._undo_log) > UNDO_HISTORY_LIMIT:
            self._undo_log = self._undo_log[-UNDO_HISTORY_LIMIT:]
        return undo_id

    def last_undoable_scan(self) -> dict[str, Any] | None:
        """Kompakter Undo-Eintrag des letzten noch nicht rückgängig gemachten Scans."""
        for entry in reversed(self._undo_log):
            if not entry["undone"]:
                return {
                    "undo_id": entry["undo_id"],
                    "ean": entry["ean"],
                    "product_name": entry["product_name"],
                    "todo_entity": entry["todo_entity"],
                    "added_item_summary": entry["added_item_summary"],
                    "action_kind": entry["action_kind"],
                    "previous_summary": entry["previous_summary"],
                    "timestamp": entry["timestamp"],
                }
        return None

    def _find_undo(self, undo_id: str | None) -> dict[str, Any] | None:
        if undo_id:
            for entry in reversed(self._undo_log):
                if entry["undo_id"] == undo_id:
                    return entry
            return None
        for entry in reversed(self._undo_log):
            if not entry["undone"]:
                return entry
        return None

    async def async_undo_scan(self, undo_id: str | None = None) -> dict[str, Any]:
        """Letzten (oder angegebenen) erfolgreichen Listen-Eintrag rückgängig machen."""
        async with self._lock:
            return await self._async_undo_scan_locked(undo_id)

    async def _async_undo_scan_locked(self, undo_id: str | None = None) -> dict[str, Any]:
        entry = self._find_undo(undo_id)
        if entry is None:
            return {
                "ok": False,
                "already_undone": False,
                "reason": "not_found" if undo_id else "nothing_to_undo",
                "undo_id": undo_id,
            }
        if entry["undone"]:
            await self._async_clear_notification_tag(success_notification_tag(entry["undo_id"]))
            return {
                "ok": False,
                "already_undone": True,
                "reason": "already_undone",
                "undo_id": entry["undo_id"],
            }

        entity = entry["todo_entity"]
        kind = entry["action_kind"]
        undone_via = None
        try:
            if kind == ACTION_INCREMENTED:
                try:
                    await self.hass.services.async_call(
                        "todo",
                        "update_item",
                        {
                            "item": entry["added_item_summary"],
                            "rename": entry["previous_summary"],
                        },
                        target={"entity_id": entity},
                        blocking=True,
                    )
                    undone_via = "update_item"
                except HomeAssistantError as err:
                    _LOGGER.debug(
                        "update_item beim Undo auf %s nicht möglich (%s), entferne Eintrag",
                        entity,
                        err,
                    )
                    await self.hass.services.async_call(
                        "todo",
                        "remove_item",
                        {"item": entry["added_item_summary"]},
                        target={"entity_id": entity},
                        blocking=True,
                    )
                    undone_via = "remove_item"
            else:
                await self.hass.services.async_call(
                    "todo",
                    "remove_item",
                    {"item": entry["added_item_summary"]},
                    target={"entity_id": entity},
                    blocking=True,
                )
                undone_via = "remove_item"
        except (HomeAssistantError, ValueError) as err:
            message = str(err)
            _LOGGER.warning("Undo für %s auf %s fehlgeschlagen: %s", entry["ean"], entity, err)
            missing = "not found" in message.casefold() or "nicht gefunden" in message.casefold()
            if not missing:
                return {
                    "ok": False,
                    "already_undone": False,
                    "reason": "update_failed" if kind == ACTION_INCREMENTED else "remove_failed",
                    "error": message,
                    "undo_id": entry["undo_id"],
                }
            undone_via = "missing"

        entry["undone"] = True
        await self._async_clear_notification_tag(success_notification_tag(entry["undo_id"]))
        self.last_result = self._result(
            entry["ean"],
            STATUS_OK,
            name=entry["product_name"],
            product=entry["product_name"],
            target_list=entity,
            action="undone",
            item=entry["previous_summary"] if kind == ACTION_INCREMENTED else None,
            undo_id=entry["undo_id"],
            undone=True,
        )
        self.async_notify_listeners()
        return {
            "ok": True,
            "already_undone": False,
            "reason": "undone",
            "undo_id": entry["undo_id"],
            "action_kind": kind,
            "ean": entry["ean"],
            "product_name": entry["product_name"],
            "todo_entity": entity,
            "undone_via": undone_via,
        }

    def get_state(self) -> dict[str, Any]:
        return {
            "products": self.storage.list_products(),
            "pending": self.storage.list_pending(),
            "last_result": self.last_result,
            "last_undoable_scan": self.last_undoable_scan(),
            "todo_entity": self.todo_entity,
            "offline_catalog_enabled": self.offline_catalog_enabled,
            "config": {k: v for k, v in self._config.items() if k != CONF_OPENGTINDB_QUERY_ID},
        }
