"""Scan-Ablauf im Manager."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from conftest import MemoryStore

from custom_components.shys_barcode_scanner.const import (
    CONF_DUPLICATE_MODE,
    CONF_ESPHOME_DEVICE_PREFIX,
    CONF_NAME_FORMAT,
    CONF_NOTIFY_ON_SUCCESS,
    CONF_NOTIFY_ONLY_HOME,
    CONF_NOTIFY_SERVICE,
    CONF_NOTIFY_SERVICES,
    CONF_QUICK_LISTS,
    CONF_TODO_ENTITY,
    CONF_UNKNOWN_MODE,
    EVENT_BARCODE_SCANNED,
    EVENT_SCAN_PROCESSED,
    SIGNAL_UPDATE,
)
from custom_components.shys_barcode_scanner.lookup import LookupResult
from custom_components.shys_barcode_scanner.manager import (
    BarcodeManager,
    build_success_notification,
    build_unknown_notification,
    clean_reply_text,
    esphome_service_name,
    find_device_tracker,
    format_item_name,
    parse_notification_action,
    should_send_notification,
    split_quantity,
    split_reply_list_suffix,
)
from custom_components.shys_barcode_scanner.storage import BarcodeStorage

EAN = "4337256722261"
LIST = "todo.shopping_list"


async def make_manager(hass, **config):
    hass.services.todo.setdefault(LIST, [])
    storage = BarcodeStorage(MemoryStore())
    await storage.async_load()
    cfg = {CONF_TODO_ENTITY: LIST, **config}
    manager = BarcodeManager(hass, cfg, storage, session=None)
    manager.lookup.async_lookup = AsyncMock(return_value=LookupResult(ean=EAN, attempts={"openfoodfacts": "not_found"}))
    return manager


def summaries(hass, entity=LIST):
    return [i["summary"] for i in hass.services.todo[entity]]


def test_helpers():
    assert format_item_name("Butter", "ja!", "brand_product") == "ja! Butter"
    assert format_item_name("Ritter Sport Marzipan", "Ritter Sport", "brand_product") == "Ritter Sport Marzipan"
    assert format_item_name("ritter sport nuss", "Ritter Sport", "brand_product") == "ritter sport nuss"
    assert format_item_name("Butter", "ja!", "product_only") == "Butter"
    assert format_item_name("Butter", "", "brand_product") == "Butter"
    assert split_quantity("3x Milch") == (3, "Milch")
    assert split_quantity("12 x Eier") == (12, "Eier")
    assert split_quantity("Milch") == (1, "Milch")
    assert split_quantity("7up") == (1, "7up")
    assert esphome_service_name("barcode-scanner") == "barcode_scanner_show_result"
    assert esphome_service_name("Küche Scanner") == "k_che_scanner_show_result"


async def test_local_hit_works_offline(hass):
    m = await make_manager(hass)
    await m.storage.upsert_product("MY-QR-KAFFEE", "Kaffee", brand="Tchibo")
    res = await m.async_handle_scan(" MY-QR-KAFFEE\r\n")
    assert res["status"] == "ok" and res["action"] == "added"
    assert summaries(hass) == ["Tchibo Kaffee"]
    m.lookup.async_lookup.assert_not_called()
    assert m.storage.get_product("MY-QR-KAFFEE")["scan_count"] == 1
    assert hass.bus.fired[-1][0] == EVENT_SCAN_PROCESSED


async def test_api_hit_auto_learns(hass):
    m = await make_manager(hass)
    m.lookup.async_lookup = AsyncMock(
        return_value=LookupResult(ean=EAN, found=True, name="Butter Streichgut gesalzen", brand="ja!", source="openfoodfacts")
    )
    res = await m.async_handle_scan(EAN)
    assert res["status"] == "ok" and res["source"] == "openfoodfacts"
    assert summaries(hass) == ["ja! Butter Streichgut gesalzen"]
    learned = m.storage.get_product(EAN)
    assert learned["source"] == "openfoodfacts" and learned["scan_count"] == 1
    # Zweiter Scan: lokal, keine API mehr
    m.lookup.async_lookup.reset_mock()
    await m.async_handle_scan(EAN)
    m.lookup.async_lookup.assert_not_called()


async def test_api_hit_real_http_cascade(hass, session):
    """Ende-zu-Ende mit echter Lookup-Klasse und gemocktem HTTP."""
    hass.services.todo[LIST] = []
    session.add(r"^https://world\.openfoodfacts\.org/", status=404)
    session.add(r"^https://ean-suche\.net/", payload={"found": True, "product_name": "Zahnpasta"})
    storage = BarcodeStorage(MemoryStore())
    m = BarcodeManager(hass, {CONF_TODO_ENTITY: LIST, CONF_NAME_FORMAT: "product_only"}, storage, session)
    res = await m.async_handle_scan("4000417025005")
    assert res["status"] == "ok" and res["source"] == "ean-suche"
    assert summaries(hass) == ["Zahnpasta"]
    assert storage.get_product("4000417025005")["source"] == "ean-suche"


async def test_duplicate_skip(hass):
    m = await make_manager(hass, **{CONF_DUPLICATE_MODE: "skip"})
    await m.storage.upsert_product(EAN, "Butter", brand="ja!")
    first = await m.async_handle_scan(EAN)
    second = await m.async_handle_scan(EAN)
    assert first["status"] == "ok"
    assert second["status"] == "duplicate" and second["action"] == "skipped"
    assert summaries(hass) == ["ja! Butter"]
    get_calls = hass.services.of("todo", "get_items")
    assert get_calls and get_calls[0][2] == {"status": ["needs_action"]}


async def test_duplicate_increment(hass):
    m = await make_manager(hass, **{CONF_DUPLICATE_MODE: "increment"})
    await m.storage.upsert_product(EAN, "Butter", brand="ja!")
    await m.async_handle_scan(EAN)
    r2 = await m.async_handle_scan(EAN)
    r3 = await m.async_handle_scan(EAN)
    assert summaries(hass) == ["3x ja! Butter"]
    assert r2["action"] == "incremented" and r2["item"] == "2x ja! Butter"
    assert r3["status"] == "ok" and r3["item"] == "3x ja! Butter"
    upd = hass.services.of("todo", "update_item")
    assert upd[0][2] == {"item": "1", "rename": "2x ja! Butter"}


async def test_duplicate_increment_ignores_completed_and_case(hass):
    m = await make_manager(hass, **{CONF_DUPLICATE_MODE: "increment"})
    hass.services.todo[LIST] = [
        {"uid": "a", "summary": "ja! butter", "status": "completed"},
        {"uid": "b", "summary": "2X JA! BUTTER", "status": "needs_action"},
    ]
    await m.storage.upsert_product(EAN, "Butter", brand="ja!")
    res = await m.async_handle_scan(EAN)
    assert res["item"] == "3x ja! Butter"
    assert summaries(hass) == ["ja! butter", "3x ja! Butter"]


async def test_increment_fallback_when_update_unsupported(hass):
    """z. B. Picnic-Warenkorb: update_item geht nicht -> erneut hinzufügen."""
    m = await make_manager(hass, **{CONF_DUPLICATE_MODE: "increment"})
    hass.services.fail_update = True
    await m.storage.upsert_product(EAN, "Butter")
    await m.async_handle_scan(EAN)
    res = await m.async_handle_scan(EAN)
    assert res["action"] == "added" and res["status"] == "ok"
    assert summaries(hass) == ["Butter", "Butter"]


async def test_duplicate_allow(hass):
    m = await make_manager(hass, **{CONF_DUPLICATE_MODE: "allow"})
    await m.storage.upsert_product(EAN, "Butter")
    await m.async_handle_scan(EAN)
    await m.async_handle_scan(EAN)
    assert summaries(hass) == ["Butter", "Butter"]
    assert not hass.services.of("todo", "get_items")


async def test_target_list_override(hass):
    m = await make_manager(hass)
    hass.services.todo["todo.drogerie"] = []
    await m.storage.upsert_product("4005900000000", "Duschgel", target_list="todo.drogerie")
    res = await m.async_handle_scan("4005900000000")
    assert res["target_list"] == "todo.drogerie"
    assert summaries(hass, "todo.drogerie") == ["Duschgel"]
    assert summaries(hass) == []


async def test_unknown_pending_notify_and_feedback(hass):
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICE: "notify.mobile_app_handy"})
    hass.services.available.add("esphome.barcode_scanner_show_result")
    res = await m.async_handle_scan(EAN, device="barcode-scanner")
    assert res["status"] == "unknown"
    assert res["name"] == ""
    assert res["ean"] == EAN
    assert [p["ean"] for p in m.storage.list_pending()] == [EAN]
    assert m.storage.list_pending()[0]["device"] == "barcode-scanner"
    assert summaries(hass) == []  # pending_only
    notify = hass.services.of("notify", "mobile_app_handy")
    assert len(notify) == 1
    assert EAN in notify[0][2]["message"] and "Barcode Scanner" in notify[0][2]["message"]
    assert notify[0][2]["data"]["url"] == "/shys-barcode-scanner"
    assert notify[0][2]["data"]["tag"] == f"shys_barcode_{EAN}"
    fb = hass.services.of("esphome", "barcode_scanner_show_result")
    assert fb[-1][2] == {"status": "unknown", "ean": EAN, "brand": "", "product": ""}
    # Zweiter Scan: kein zweites Pushen, Zähler hoch
    await m.async_handle_scan(EAN, device="barcode-scanner")
    assert len(hass.services.of("notify")) == 1
    assert m.storage.list_pending()[0]["scan_count"] == 2


async def test_unknown_add_ean_and_pending(hass):
    m = await make_manager(hass, **{CONF_UNKNOWN_MODE: "add_ean_and_pending"})
    res = await m.async_handle_scan(EAN)
    assert res["status"] == "unknown" and res["action"] == "added"
    assert summaries(hass) == [EAN]
    assert m.storage.pending


async def test_custom_code_unknown_skips_api(hass):
    m = await make_manager(hass)
    res = await m.async_handle_scan("QR:BROETCHEN")
    assert res["status"] == "unknown"
    m.lookup.async_lookup.assert_not_called()
    assert "QR:BROETCHEN" in m.storage.pending


async def test_invalid_checksum_skips_api(hass):
    m = await make_manager(hass)
    await m.async_handle_scan("4337256722262")
    m.lookup.async_lookup.assert_not_called()


async def test_offline_lookup_reports_error(hass):
    m = await make_manager(hass)
    m.lookup.async_lookup = AsyncMock(
        return_value=LookupResult(ean=EAN, attempts={"openfoodfacts": "failed: ClientError", "ean-suche": "failed: Timeout"})
    )
    res = await m.async_handle_scan(EAN)
    assert res["status"] == "error"
    assert EAN in m.storage.pending


async def test_feedback_ok_with_prefix_and_missing_service(hass):
    m = await make_manager(hass, **{CONF_ESPHOME_DEVICE_PREFIX: "barcode-scanner"})
    await m.storage.upsert_product(EAN, "Butter", brand="ja!")
    # Aktion existiert nicht -> kein Aufruf, kein Fehler
    await m.async_handle_scan(EAN)
    assert not hass.services.of("esphome")
    hass.services.available.add("esphome.barcode_scanner_show_result")
    await m.async_handle_scan(EAN)
    fb = hass.services.of("esphome", "barcode_scanner_show_result")
    assert fb[0][2] == {"status": "ok", "ean": EAN, "brand": "ja!", "product": "Butter"}


async def test_todo_error_status(hass):
    m = await make_manager(hass, **{CONF_TODO_ENTITY: "todo.gibts_nicht"})
    await m.storage.upsert_product(EAN, "Butter")
    res = await m.async_handle_scan(EAN)
    assert res["status"] == "error"
    assert m.last_result["status"] == "error"


async def test_unexpected_exception_is_contained(hass):
    m = await make_manager(hass)
    m.lookup.async_lookup = AsyncMock(side_effect=RuntimeError("kaputt"))
    res = await m.async_handle_scan(EAN)
    assert res["status"] == "error"


async def test_empty_code(hass):
    m = await make_manager(hass)
    res = await m.async_handle_scan("\r\n")
    assert res["status"] == "error"
    assert summaries(hass) == []


async def test_event_listener_and_dispatcher(hass):
    from homeassistant.core import Event
    from homeassistant.helpers import dispatcher

    m = await make_manager(hass)
    await m.storage.upsert_product(EAN, "Butter")
    m.async_start()
    assert len(hass.bus.listeners[EVENT_BARCODE_SCANNED]) == 1
    listener = hass.bus.listeners[EVENT_BARCODE_SCANNED][0]
    listener(Event(EVENT_BARCODE_SCANNED, {"ean": EAN, "device": "barcode-scanner"}))
    listener(Event(EVENT_BARCODE_SCANNED, {"foo": "bar"}))  # ohne ean -> ignoriert
    await asyncio.gather(*hass.tasks)
    assert len(hass.tasks) == 1
    assert summaries(hass) == ["Butter"]
    assert m.last_result["device"] == "barcode-scanner"
    if hasattr(dispatcher, "SENT"):
        assert SIGNAL_UPDATE in dispatcher.SENT
    m.async_stop()
    assert hass.bus.listeners[EVENT_BARCODE_SCANNED] == []


async def test_concurrent_scans_serialized(hass):
    m = await make_manager(hass, **{CONF_DUPLICATE_MODE: "increment"})
    await m.storage.upsert_product(EAN, "Butter")
    await asyncio.gather(*(m.async_handle_scan(EAN) for _ in range(4)))
    assert summaries(hass) == ["4x Butter"]


async def test_learn_delete_dismiss_import(hass):
    m = await make_manager(hass)
    await m.async_handle_scan("CUSTOM-1")
    assert "CUSTOM-1" in m.storage.pending
    out = await m.async_learn("CUSTOM-1", "Brötchen", add_to_list=True)
    assert out["product"]["name"] == "Brötchen"
    assert out["scan"]["status"] == "ok"
    assert summaries(hass) == ["Brötchen"]
    assert m.storage.pending == {}
    assert await m.async_delete("CUSTOM-1")
    await m.async_handle_scan("CUSTOM-2")
    assert await m.async_dismiss("CUSTOM-2")
    stats = await m.async_import({"products": {"X": {"name": "Ix"}}})
    assert stats["imported"] == 1
    state = m.get_state()
    assert [p["ean"] for p in state["products"]] == ["X"]
    assert state["todo_entity"] == LIST
    assert "opengtindb_query_id" not in state["config"]


async def test_update_config(hass):
    m = await make_manager(hass)
    m.update_config({CONF_TODO_ENTITY: "todo.picnic_warenkorb", "use_openfoodfacts": False, "opengtindb_query_id": "42"})
    assert m.todo_entity == "todo.picnic_warenkorb"
    assert not m.lookup.use_openfoodfacts and m.lookup.opengtindb_query_id == "42"


# ---------------------------------------------------------------- Push-Antwort (v1.1.0)
NOTIFY = "notify.mobile_app_handy"
ACTION_EVENT = "mobile_app_notification_action"


async def fire_action(hass, m, data):
    from homeassistant.core import Event

    for listener in list(hass.bus.listeners.get(ACTION_EVENT, [])):
        listener(Event(ACTION_EVENT, data))
    await asyncio.gather(*hass.tasks)


def clears(hass):
    return [c for c in hass.services.of("notify", "mobile_app_handy") if c[2].get("message") == "clear_notification"]


def test_notification_payload():
    p = build_unknown_notification(EAN)
    assert p["title"] == "Unbekannter Barcode"
    assert EAN in p["message"] and "Produktnamen" in p["message"]
    d = p["data"]
    assert d["tag"] == f"shys_barcode_{EAN}"
    assert d["url"] == "/shys-barcode-scanner" and d["clickAction"] == "/shys-barcode-scanner"
    assert d["action_data"] == {"shys_ean": EAN}
    reply, dismiss = d["actions"]
    assert reply["action"] == f"SHYS_REPLY_{EAN}" and reply["behavior"] == "textInput"
    assert reply["title"] == "Produkt benennen"
    assert reply["textInputButtonTitle"] and reply["textInputPlaceholder"]
    assert dismiss == {"action": f"SHYS_DISMISS_{EAN}", "title": "Verwerfen", "destructive": True}


def test_parse_notification_action():
    assert parse_notification_action({"action": f"SHYS_REPLY_{EAN}"}) == {"kind": "reply", "ean": EAN}
    assert parse_notification_action({"action": f"SHYS_DISMISS_{EAN}"}) == {"kind": "dismiss", "ean": EAN}
    assert parse_notification_action({"action": "REPLY", "tag": f"shys_barcode_{EAN}"}) == {"kind": "reply", "ean": EAN}
    assert parse_notification_action({"action": "REPLY", "action_data": {"shys_ean": EAN}}) == {"kind": "reply", "ean": EAN}
    assert parse_notification_action({"action": f"SHYS_REPLY_L0_{EAN}"}) == {
        "kind": "reply", "ean": EAN, "list_index": 0
    }
    assert parse_notification_action(
        {"action": f"SHYS_REPLY_L1_{EAN}", "action_data": {"shys_ean": EAN, "shys_target_list": "todo.drogerie"}}
    ) == {"kind": "reply", "ean": EAN, "list_index": 1, "target_list": "todo.drogerie"}
    assert parse_notification_action({"action": "SHYS_UNDO_abc123"}) == {"kind": "undo", "undo_id": "abc123"}
    # fremde Aktionen / Tags
    assert parse_notification_action({"action": "REPLY", "tag": "other_automation"}) is None
    assert parse_notification_action({"action": "REPLY"}) is None
    assert parse_notification_action({"action": "REPLY", "tag": ""}) is None
    assert parse_notification_action({"action": "ALARM"}) is None
    assert parse_notification_action({"action": "SHYS_REPLY_"}) is None
    assert parse_notification_action({"action": "REPLY", "tag": "shys_barcode_"}) is None
    assert parse_notification_action({"action": "REPLY", "tag": "shys_barcode_ok_abc"}) is None
    assert parse_notification_action({}) is None
    assert clean_reply_text("  Hafer   milch \n") == "Hafer milch"
    assert clean_reply_text(None) == "" and clean_reply_text("null") == "" and clean_reply_text("   ") == ""
    assert split_reply_list_suffix("Spülmittel @Drogerie") == ("Spülmittel", "Drogerie")
    assert split_reply_list_suffix("Spülmittel") == ("Spülmittel", None)


async def test_unknown_sends_actionable_notification(hass):
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICE: NOTIFY})
    await m.async_handle_scan(EAN)
    (call,) = hass.services.of("notify", "mobile_app_handy")
    assert call[2] == build_unknown_notification(EAN)


async def test_reply_with_tag_learns_and_adds(hass):
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICE: NOTIFY})
    m.async_start()
    await m.async_handle_scan(EAN)
    assert EAN in m.storage.pending
    await fire_action(hass, m, {"action": "REPLY", "tag": f"shys_barcode_{EAN}", "reply_text": " Hafermilch "})
    assert m.storage.get_product(EAN)["name"] == "Hafermilch"
    assert m.storage.get_product(EAN)["source"] == "notification"
    assert EAN not in m.storage.pending
    assert summaries(hass) == ["Hafermilch"]
    assert m.last_result["status"] == "ok"
    assert m.last_result["name"] == "Hafermilch"
    assert m.last_result["ean"] == EAN
    (clear,) = clears(hass)
    assert clear[2]["data"] == {"tag": f"shys_barcode_{EAN}"}


async def test_learn_without_list_updates_matching_last_result_only(hass):
    """Panel-Anlernen ohne Liste aktualisiert last_result, lässt aber einen fremden neueren Scan unberührt."""
    m = await make_manager(hass)
    await m.async_handle_scan(EAN)
    assert m.last_result["status"] == "unknown" and m.last_result["name"] == ""
    await m.async_learn(EAN, "Hafermilch", brand="ja!", add_to_list=False)
    assert EAN not in m.storage.pending
    assert summaries(hass) == []
    assert m.last_result["status"] == "ok"
    assert m.last_result["name"] == "ja! Hafermilch"
    assert m.last_result["ean"] == EAN
    assert m.last_result["product"] == "Hafermilch"

    other = await m.async_handle_scan("QR-ANDERER")
    assert other["status"] == "unknown"
    assert m.last_result["ean"] == "QR-ANDERER"
    await m.async_learn(EAN, "Haferdrink", brand="Oatly", add_to_list=False)
    assert m.storage.get_product(EAN)["name"] == "Haferdrink"
    assert m.last_result["ean"] == "QR-ANDERER"
    assert m.last_result["status"] == "unknown"


async def test_already_known_reply_updates_unknown_last_result(hass):
    """Zweite Push-Antwort räumt pending und ein noch unbekanntes last_result auf."""
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICE: NOTIFY})
    m.async_start()
    await m.async_handle_scan(EAN)
    await m.storage.upsert_product(EAN, "Hafermilch", brand="ja!", source="manual")
    assert m.last_result["status"] == "unknown"
    await fire_action(hass, m, {"action": f"SHYS_REPLY_{EAN}", "reply_text": "Ignoriert"})
    assert m.last_result["status"] == "ok"
    assert m.last_result["name"] == "ja! Hafermilch"
    assert EAN not in m.storage.pending
    # Schon bekannt: kein zweites Hinzufügen zur Liste
    assert summaries(hass) == []


async def test_reply_with_own_action_id_ios_and_duplicate_mode(hass):
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICE: NOTIFY, CONF_DUPLICATE_MODE: "increment"})
    hass.services.todo[LIST].append({"uid": "x", "summary": "Hafermilch", "status": "needs_action"})
    m.async_start()
    await m.async_handle_scan(EAN)
    # iOS: kein tag im Event, aber eigene Aktions-ID + action_data
    await fire_action(hass, m, {"action": f"SHYS_REPLY_{EAN}", "reply_text": "Hafermilch", "action_data": {"shys_ean": EAN}})
    assert summaries(hass) == ["2x Hafermilch"]
    assert m.storage.pending == {}


async def test_reply_with_unrelated_tag_is_ignored(hass):
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICE: NOTIFY})
    m.async_start()
    await m.async_handle_scan(EAN)
    calls_before = len(hass.services.calls)
    await fire_action(hass, m, {"action": "REPLY", "tag": "other_automation", "reply_text": "Hallo"})
    await fire_action(hass, m, {"action": "REPLY", "reply_text": "Hallo"})  # ohne tag (z. B. iOS, fremd)
    assert hass.tasks == []
    assert m.storage.get_product(EAN) is None
    assert EAN in m.storage.pending
    assert summaries(hass) == []
    assert len(hass.services.calls) == calls_before


async def test_reply_empty_text_is_safe(hass):
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICE: NOTIFY})
    m.async_start()
    await m.async_handle_scan(EAN)
    for text in ("", "   ", None, "null"):
        await fire_action(hass, m, {"action": f"SHYS_REPLY_{EAN}", "tag": f"shys_barcode_{EAN}", "reply_text": text})
    await fire_action(hass, m, {"action": f"SHYS_REPLY_{EAN}"})  # ganz ohne reply_text
    assert m.storage.get_product(EAN) is None
    assert EAN in m.storage.pending
    assert summaries(hass) == []
    assert clears(hass) == []


async def test_reply_errors_are_contained(hass):
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICE: NOTIFY})
    m.async_learn = AsyncMock(side_effect=RuntimeError("boom"))
    assert await m.async_handle_notification_action("reply", EAN, "Milch") is None


async def test_dismiss_action_removes_pending(hass):
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICE: NOTIFY})
    m.async_start()
    await m.async_handle_scan(EAN)
    await fire_action(hass, m, {"action": f"SHYS_DISMISS_{EAN}", "tag": f"shys_barcode_{EAN}"})
    assert m.storage.pending == {}
    assert m.storage.get_product(EAN) is None
    assert summaries(hass) == []
    (clear,) = clears(hass)
    assert clear[2] == {"message": "clear_notification", "data": {"tag": f"shys_barcode_{EAN}"}}


async def test_action_without_notify_service_and_clear_failure(hass):
    # ohne notify_service: lernen klappt, kein clear
    m = await make_manager(hass)
    m.async_start()
    await m.async_handle_scan(EAN)
    await fire_action(hass, m, {"action": f"SHYS_REPLY_{EAN}", "reply_text": "Hafermilch"})
    assert summaries(hass) == ["Hafermilch"] and hass.services.of("notify") == []

    # clear_notification wirft -> best effort, kein Fehler
    async def failing_call(domain, service, data=None, **kw):
        if domain == "notify":
            raise RuntimeError("offline")
        return await original(domain, service, data, **kw)

    original = hass.services.async_call
    hass.services.async_call = failing_call
    m.update_config({**m.config, CONF_NOTIFY_SERVICE: NOTIFY})
    await m.storage.add_pending("CUSTOM-9")
    out = await m.async_handle_notification_action("dismiss", "CUSTOM-9")
    assert out == {"ean": "CUSTOM-9", "dismissed": True}
    assert m.storage.pending == {}


async def test_unload_removes_notification_listener(hass):
    m = await make_manager(hass)
    m.async_start()
    assert len(hass.bus.listeners[ACTION_EVENT]) == 1
    m.async_stop()
    assert hass.bus.listeners[ACTION_EVENT] == []
    assert hass.bus.listeners[EVENT_BARCODE_SCANNED] == []
    m.async_stop()  # doppelt stoppen ist harmlos


# ---------------------------------------------------------------- Mehrere Handys / Anwesenheit (v1.2.0)
PHONE_A = "notify.mobile_app_handy"
PHONE_B = "notify.mobile_app_pixel_frau"
GROUP = "notify.familie"


def notify_calls(hass, service=None, *, clears_only=None):
    calls = hass.services.of("notify", service)
    if clears_only is True:
        return [c for c in calls if c[2].get("message") == "clear_notification"]
    if clears_only is False:
        return [c for c in calls if c[2].get("message") != "clear_notification"]
    return calls


async def test_legacy_notify_service_still_works(hass):
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICE: "  notify.mobile_app_handy "})
    await m.async_handle_scan(EAN)
    assert len(notify_calls(hass, "mobile_app_handy", clears_only=False)) == 1


async def test_notify_multiple_phones_and_partial_failure(hass):
    from homeassistant.exceptions import HomeAssistantError

    m = await make_manager(hass, **{CONF_NOTIFY_SERVICES: [PHONE_A, PHONE_B]})
    original = hass.services.async_call

    async def flaky(domain, service, data=None, **kw):
        if domain == "notify" and service == "mobile_app_handy":
            raise HomeAssistantError("offline")
        return await original(domain, service, data, **kw)

    hass.services.async_call = flaky
    await m.async_handle_scan(EAN)
    assert notify_calls(hass, "mobile_app_handy") == []
    assert len(notify_calls(hass, "mobile_app_pixel_frau", clears_only=False)) == 1
    assert notify_calls(hass, "mobile_app_pixel_frau")[0][2] == build_unknown_notification(EAN)


async def test_notify_only_home_person_states(hass):
    hass.states.async_set("device_tracker.handy", "home")
    hass.states.async_set("device_tracker.pixel_frau", "not_home")
    hass.states.async_set("person.kalle", "home", {"device_trackers": ["device_tracker.handy"]})
    hass.states.async_set("person.anna", "not_home", {"device_trackers": ["device_tracker.pixel_frau"]})
    m = await make_manager(
        hass,
        **{CONF_NOTIFY_SERVICES: [PHONE_A, PHONE_B], CONF_NOTIFY_ONLY_HOME: True},
    )
    await m.async_handle_scan(EAN)
    assert len(notify_calls(hass, "mobile_app_handy", clears_only=False)) == 1
    assert notify_calls(hass, "mobile_app_pixel_frau") == []


async def test_notify_only_home_other_zone_and_tracker_fallback(hass):
    # Keine person.* fürs Handy -> Fallback auf device_tracker
    hass.states.async_set("device_tracker.handy", "Arbeit")
    m = await make_manager(
        hass, **{CONF_NOTIFY_SERVICES: [PHONE_A], CONF_NOTIFY_ONLY_HOME: True}
    )
    await m.async_handle_scan(EAN)
    assert notify_calls(hass, "mobile_app_handy") == []

    # Tracker zuhause, ohne Person -> senden
    hass.states.async_set("device_tracker.handy", "home")
    await m.async_handle_scan("QR-HOME")
    assert len(notify_calls(hass, "mobile_app_handy", clears_only=False)) == 1


async def test_notify_only_home_fail_open_unknown_and_group(hass):
    # Kein Tracker zum Handy -> fail open
    m = await make_manager(
        hass, **{CONF_NOTIFY_SERVICES: [PHONE_A, GROUP], CONF_NOTIFY_ONLY_HOME: True}
    )
    await m.async_handle_scan(EAN)
    assert len(notify_calls(hass, "mobile_app_handy", clears_only=False)) == 1
    assert len(notify_calls(hass, "familie", clears_only=False)) == 1

    # Unklarer Status (unavailable) -> fail open
    hass.services.calls.clear()
    hass.states.async_set("device_tracker.handy", "unavailable")
    await m.async_handle_scan("QR-UNAVAIL")
    assert len(notify_calls(hass, "mobile_app_handy", clears_only=False)) == 1


async def test_find_device_tracker_via_registry(hass):
    device = hass.device_registry.add("dev-1", name="Pixel Frau")
    hass.entity_registry.add("device_tracker.anna_handy", platform="mobile_app", device_id=device.id)
    # Kein State unter device_tracker.pixel_frau
    assert find_device_tracker(hass, "pixel_frau") == "device_tracker.anna_handy"
    hass.states.async_set("device_tracker.anna_handy", "home")
    assert should_send_notification(hass, PHONE_B, True) is True
    hass.states.async_set("device_tracker.anna_handy", "not_home")
    assert should_send_notification(hass, PHONE_B, True) is False
    # ohne Filter immer senden
    assert should_send_notification(hass, PHONE_B, False) is True


async def test_clear_notification_on_all_phones_for_push_and_panel(hass):
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICES: [PHONE_A, PHONE_B]})
    await m.async_handle_scan(EAN)
    await m.async_learn(EAN, "Hafermilch", add_to_list=True)
    clears_a = notify_calls(hass, "mobile_app_handy", clears_only=True)
    clears_b = notify_calls(hass, "mobile_app_pixel_frau", clears_only=True)
    assert len(clears_a) == 1 and len(clears_b) == 1
    expected = {"message": "clear_notification", "data": {"tag": f"shys_barcode_{EAN}"}}
    assert clears_a[0][2] == expected == clears_b[0][2]
    assert summaries(hass) == ["Hafermilch"]

    # Panel/Service-Verwerfen räumt ebenfalls auf allen Handys auf
    await m.async_handle_scan("QR-WEG")
    assert await m.async_dismiss("QR-WEG")
    assert [c[2]["data"]["tag"] for c in notify_calls(hass, "mobile_app_handy", clears_only=True)] == [
        f"shys_barcode_{EAN}",
        "shys_barcode_QR-WEG",
    ]
    assert [c[2]["data"]["tag"] for c in notify_calls(hass, "mobile_app_pixel_frau", clears_only=True)] == [
        f"shys_barcode_{EAN}",
        "shys_barcode_QR-WEG",
    ]


async def test_push_reply_clears_all_even_if_only_one_was_notified(hass):
    hass.states.async_set("device_tracker.handy", "home")
    hass.states.async_set("person.kalle", "home", {"device_trackers": ["device_tracker.handy"]})
    hass.states.async_set("device_tracker.pixel_frau", "not_home")
    hass.states.async_set("person.anna", "not_home", {"device_trackers": ["device_tracker.pixel_frau"]})
    m = await make_manager(
        hass,
        **{CONF_NOTIFY_SERVICES: [PHONE_A, PHONE_B], CONF_NOTIFY_ONLY_HOME: True},
    )
    m.async_start()
    await m.async_handle_scan(EAN)
    assert notify_calls(hass, "mobile_app_pixel_frau", clears_only=False) == []
    await fire_action(hass, m, {"action": f"SHYS_REPLY_{EAN}", "reply_text": "Milch"})
    # Clear geht trotzdem an beide konfigurierten Handys
    assert len(notify_calls(hass, "mobile_app_handy", clears_only=True)) == 1
    assert len(notify_calls(hass, "mobile_app_pixel_frau", clears_only=True)) == 1


async def test_duplicate_push_replies_are_idempotent(hass):
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICES: [PHONE_A], CONF_DUPLICATE_MODE: "increment"})
    m.async_start()
    await m.async_handle_scan(EAN)
    from homeassistant.core import Event

    for listener in list(hass.bus.listeners.get(ACTION_EVENT, [])):
        listener(Event(ACTION_EVENT, {"action": f"SHYS_REPLY_{EAN}", "reply_text": "Hafermilch"}))
        listener(Event(ACTION_EVENT, {"action": f"SHYS_REPLY_{EAN}", "reply_text": "Haferdrink"}))
    await asyncio.gather(*hass.tasks)
    # Erste Antwort gewinnt, zweite crasht nicht und zählt die Menge nicht hoch
    assert m.storage.get_product(EAN)["name"] == "Hafermilch"
    assert summaries(hass) == ["Hafermilch"]
    assert EAN not in m.storage.pending


async def test_clear_failure_on_one_phone_does_not_block_the_other(hass):
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICES: [PHONE_A, PHONE_B]})
    await m.storage.add_pending(EAN)
    original = hass.services.async_call

    async def flaky(domain, service, data=None, **kw):
        if domain == "notify" and service == "mobile_app_handy" and (data or {}).get("message") == "clear_notification":
            raise RuntimeError("offline")
        return await original(domain, service, data, **kw)

    hass.services.async_call = flaky
    assert await m.async_dismiss(EAN) is True
    assert m.storage.pending == {}
    assert len(notify_calls(hass, "mobile_app_pixel_frau", clears_only=True)) == 1


# ---------------------------------------------------------------- Undo / Listenwahl / Erfolgs-Push (v1.3.0)
DROGERIE = "todo.drogerie"


def success_notifies(hass, service="mobile_app_handy"):
    return [
        c
        for c in notify_calls(hass, service, clears_only=False)
        if c[2].get("title") == "Auf der Einkaufsliste"
    ]


async def test_undo_after_add(hass):
    m = await make_manager(hass)
    await m.storage.upsert_product(EAN, "Milch")
    res = await m.async_handle_scan(EAN)
    assert res["action"] == "added" and res["undo_id"]
    assert summaries(hass) == ["Milch"]
    assert m.last_undoable_scan()["undo_id"] == res["undo_id"]
    out = await m.async_undo_scan(res["undo_id"])
    assert out["ok"] is True and out["reason"] == "undone" and out["undone_via"] == "remove_item"
    assert summaries(hass) == []
    assert m.last_undoable_scan() is None
    assert m.last_result["action"] == "undone"
    assert m.get_state()["last_undoable_scan"] is None
    assert hass.services.of("todo", "remove_item")


async def test_undo_after_increment_restores_previous_summary(hass):
    m = await make_manager(hass, **{CONF_DUPLICATE_MODE: "increment"})
    await m.storage.upsert_product(EAN, "Butter", brand="ja!")
    first = await m.async_handle_scan(EAN)
    second = await m.async_handle_scan(EAN)
    assert summaries(hass) == ["2x ja! Butter"]
    assert second["action"] == "incremented"
    out = await m.async_undo_scan(second["undo_id"])
    assert out["ok"] is True and out["undone_via"] == "update_item"
    assert summaries(hass) == ["ja! Butter"]
    upd = hass.services.of("todo", "update_item")
    assert upd[-1][2] == {"item": "2x ja! Butter", "rename": "ja! Butter"}
    # Der erste Scan bleibt undo-bar
    assert m.last_undoable_scan()["undo_id"] == first["undo_id"]
    await m.async_undo_scan()
    assert summaries(hass) == []


async def test_undo_increment_if_get_items_returns_live_references(hass):
    """previous_summary darf nicht die mutierte Liste nach update_item übernehmen."""
    original = hass.services.async_call

    async def live(domain, service, data=None, **kw):
        result = await original(domain, service, data, **kw)
        if domain == "todo" and service == "get_items":
            entity = (kw.get("target") or {}).get("entity_id")
            return {entity: {"items": hass.services.todo[entity]}}
        return result

    hass.services.async_call = live
    m = await make_manager(hass, **{CONF_DUPLICATE_MODE: "increment"})
    await m.storage.upsert_product(EAN, "Milch")
    await m.async_handle_scan(EAN)
    second = await m.async_handle_scan(EAN)
    assert summaries(hass) == ["2x Milch"]
    out = await m.async_undo_scan(second["undo_id"])
    assert out["ok"] is True
    assert summaries(hass) == ["Milch"]
    assert hass.services.of("todo", "update_item")[-1][2] == {"item": "2x Milch", "rename": "Milch"}


async def test_undo_increment_falls_back_to_remove(hass):
    m = await make_manager(hass, **{CONF_DUPLICATE_MODE: "increment"})
    await m.storage.upsert_product(EAN, "Milch")
    await m.async_handle_scan(EAN)
    second = await m.async_handle_scan(EAN)
    hass.services.fail_update = True
    out = await m.async_undo_scan(second["undo_id"])
    assert out["ok"] is True and out["undone_via"] == "remove_item"
    assert summaries(hass) == []


async def test_undo_via_push_button_clears_and_is_idempotent(hass):
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICES: [PHONE_A, PHONE_B], CONF_NOTIFY_ON_SUCCESS: True})
    hass.states.async_set(LIST, "0", {"friendly_name": "Einkaufsliste"})
    m.async_start()
    await m.storage.upsert_product(EAN, "Milch")
    res = await m.async_handle_scan(EAN)
    undo_id = res["undo_id"]
    assert success_notifies(hass, "mobile_app_handy")
    assert success_notifies(hass, "mobile_app_pixel_frau")
    payload = success_notifies(hass, "mobile_app_handy")[0][2]
    assert payload["data"]["tag"] == f"shys_barcode_ok_{undo_id}"
    assert payload["data"]["actions"] == [{"action": f"SHYS_UNDO_{undo_id}", "title": "Rückgängig"}]
    await fire_action(hass, m, {"action": f"SHYS_UNDO_{undo_id}"})
    assert summaries(hass) == []
    clears = [
        c
        for c in notify_calls(hass, "mobile_app_handy", clears_only=True)
        if c[2]["data"]["tag"] == f"shys_barcode_ok_{undo_id}"
    ]
    assert len(clears) == 1
    assert len(notify_calls(hass, "mobile_app_pixel_frau", clears_only=True)) == 1
    # Zweiter Klick: bereits rückgängig, kein zweites remove_item, Notification wird erneut ausgeblendet
    removes_before = len(hass.services.of("todo", "remove_item"))
    second = await m.async_handle_notification_action("undo", "", undo_id=undo_id)
    assert second["already_undone"] is True and second["ok"] is False
    assert len(hass.services.of("todo", "remove_item")) == removes_before
    assert summaries(hass) == []


async def test_notify_on_success_default_off(hass):
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICES: [PHONE_A]})
    await m.storage.upsert_product(EAN, "Milch")
    await m.async_handle_scan(EAN)
    assert success_notifies(hass) == []
    assert notify_calls(hass, "mobile_app_handy") == []


async def test_notify_on_success_respects_only_home(hass):
    hass.states.async_set("device_tracker.handy", "home")
    hass.states.async_set("person.kalle", "home", {"device_trackers": ["device_tracker.handy"]})
    hass.states.async_set("device_tracker.pixel_frau", "not_home")
    hass.states.async_set("person.anna", "not_home", {"device_trackers": ["device_tracker.pixel_frau"]})
    hass.states.async_set(LIST, "0", {"friendly_name": "Einkaufsliste"})
    m = await make_manager(
        hass,
        **{
            CONF_NOTIFY_SERVICES: [PHONE_A, PHONE_B],
            CONF_NOTIFY_ONLY_HOME: True,
            CONF_NOTIFY_ON_SUCCESS: True,
        },
    )
    await m.storage.upsert_product(EAN, "Milch")
    res = await m.async_handle_scan(EAN)
    assert len(success_notifies(hass, "mobile_app_handy")) == 1
    assert "Milch wurde zu Einkaufsliste hinzugefügt." in success_notifies(hass, "mobile_app_handy")[0][2]["message"]
    assert success_notifies(hass, "mobile_app_pixel_frau") == []
    # Clear nach Undo geht trotzdem an beide
    await m.async_undo_scan(res["undo_id"])
    assert len(notify_calls(hass, "mobile_app_handy", clears_only=True)) == 1
    assert len(notify_calls(hass, "mobile_app_pixel_frau", clears_only=True)) == 1


async def test_unknown_push_fall_a_stays_two_buttons(hass):
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICES: [PHONE_A], CONF_QUICK_LISTS: [LIST]})
    await m.async_handle_scan(EAN)
    payload = notify_calls(hass, "mobile_app_handy", clears_only=False)[0][2]
    assert payload == build_unknown_notification(EAN)
    assert [a["action"] for a in payload["data"]["actions"]] == [f"SHYS_REPLY_{EAN}", f"SHYS_DISMISS_{EAN}"]


async def test_unknown_push_fall_b_two_list_buttons(hass):
    hass.states.async_set(LIST, "0", {"friendly_name": "Einkaufsliste"})
    hass.states.async_set(DROGERIE, "0", {"friendly_name": "Drogerie"})
    hass.services.todo[DROGERIE] = []
    m = await make_manager(
        hass,
        **{CONF_NOTIFY_SERVICES: [PHONE_A], CONF_QUICK_LISTS: [DROGERIE, LIST]},
    )
    await m.async_handle_scan(EAN)
    payload = notify_calls(hass, "mobile_app_handy", clears_only=False)[0][2]
    actions = payload["data"]["actions"]
    assert len(actions) == 3
    assert actions[0]["action"] == f"SHYS_REPLY_L0_{EAN}"
    assert actions[0]["title"] == "→ Einkaufsliste"
    assert actions[0]["behavior"] == "textInput"
    assert actions[0]["action_data"] == {"shys_ean": EAN, "shys_target_list": LIST}
    assert actions[1]["action"] == f"SHYS_REPLY_L1_{EAN}"
    assert actions[1]["title"] == "→ Drogerie"
    assert actions[1]["action_data"]["shys_target_list"] == DROGERIE
    assert actions[2] == {"action": f"SHYS_DISMISS_{EAN}", "title": "Verwerfen", "destructive": True}
    assert m._pending_list_choices[EAN] == [LIST, DROGERIE]


async def test_reply_l1_saves_target_list_and_adds(hass):
    hass.states.async_set(LIST, "0", {"friendly_name": "Einkaufsliste"})
    hass.states.async_set(DROGERIE, "0", {"friendly_name": "Drogerie"})
    hass.services.todo[DROGERIE] = []
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICES: [PHONE_A], CONF_QUICK_LISTS: [DROGERIE]})
    m.async_start()
    await m.async_handle_scan(EAN)
    # Ohne action_data: Fallback über pending_list_choices
    await fire_action(hass, m, {"action": f"SHYS_REPLY_L1_{EAN}", "reply_text": "Duschgel"})
    product = m.storage.get_product(EAN)
    assert product["name"] == "Duschgel" and product["target_list"] == DROGERIE
    assert summaries(hass, DROGERIE) == ["Duschgel"]
    assert summaries(hass) == []
    assert EAN not in m.storage.pending
    assert EAN not in m._pending_list_choices


async def test_reply_at_list_suffix(hass):
    hass.states.async_set(LIST, "0", {"friendly_name": "Einkaufsliste"})
    hass.states.async_set(DROGERIE, "0", {"friendly_name": "Drogerie"})
    hass.services.todo[DROGERIE] = []
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICES: [PHONE_A], CONF_QUICK_LISTS: [DROGERIE]})
    m.async_start()
    await m.async_handle_scan(EAN)
    await fire_action(hass, m, {"action": f"SHYS_REPLY_{EAN}", "reply_text": "Spülmittel @Drogerie"})
    product = m.storage.get_product(EAN)
    assert product["name"] == "Spülmittel" and product["target_list"] == DROGERIE
    assert summaries(hass, DROGERIE) == ["Spülmittel"]
    assert summaries(hass) == []


async def test_reply_at_suffix_unknown_keeps_text(hass):
    m = await make_manager(hass, **{CONF_NOTIFY_SERVICES: [PHONE_A]})
    m.async_start()
    await m.async_handle_scan(EAN)
    await fire_action(hass, m, {"action": f"SHYS_REPLY_{EAN}", "reply_text": "Käse @Unbekannt"})
    product = m.storage.get_product(EAN)
    assert product["name"] == "Käse @Unbekannt"
    assert not product.get("target_list")
    assert summaries(hass) == ["Käse @Unbekannt"]


async def test_build_success_notification_payload():
    payload = build_success_notification("abc", "Milch", "Einkaufsliste")
    assert payload["title"] == "Auf der Einkaufsliste"
    assert payload["message"] == "Milch wurde zu Einkaufsliste hinzugefügt."
    assert payload["data"]["tag"] == "shys_barcode_ok_abc"
    assert payload["data"]["actions"] == [{"action": "SHYS_UNDO_abc", "title": "Rückgängig"}]
