"""Echte HA-Instanz: Config-Flow, Options-Flow, Setup, Event-Ablauf, Services, WebSocket, Panel."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant import config_entries
from homeassistant.components import frontend
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.shys_barcode_scanner.const import DOMAIN, PANEL_URL_PATH
from custom_components.shys_barcode_scanner.lookup import LookupResult

EAN = "4337256722261"
LIST = "todo.shopping_list"


def _entities(call: ServiceCall) -> list[str]:
    value = call.data["entity_id"]
    return [value] if isinstance(value, str) else list(value)


class TodoFake:
    """Registriert todo.add_item/get_items/update_item als einfache Services."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.items: dict[str, list[dict[str, Any]]] = {LIST: []}
        self.uid = 0

        async def add_item(call: ServiceCall):
            for entity in _entities(call):
                self.uid += 1
                self.items.setdefault(entity, []).append(
                    {"uid": str(self.uid), "summary": call.data["item"], "status": "needs_action"}
                )

        async def get_items(call: ServiceCall):
            return {
                e: {"items": [dict(i) for i in self.items.get(e, []) if i["status"] in call.data.get("status", ["needs_action"])]}
                for e in _entities(call)
            }

        async def update_item(call: ServiceCall):
            for entity in _entities(call):
                for i in self.items.get(entity, []):
                    if call.data["item"] in (i["uid"], i["summary"]):
                        i["summary"] = call.data["rename"]
                        return
                raise HomeAssistantError(f"item not found: {call.data.get('item')}")

        async def remove_item(call: ServiceCall):
            for entity in _entities(call):
                items = self.items.setdefault(entity, [])
                for i in list(items):
                    if call.data["item"] in (i["uid"], i["summary"]):
                        items.remove(i)
                        return
                raise HomeAssistantError(f"item not found: {call.data.get('item')}")

        hass.services.async_register("todo", "add_item", add_item)
        hass.services.async_register("todo", "get_items", get_items, supports_response=SupportsResponse.ONLY)
        hass.services.async_register("todo", "update_item", update_item)
        hass.services.async_register("todo", "remove_item", remove_item)

    def summaries(self, entity: str = LIST) -> list[str]:
        return [i["summary"] for i in self.items[entity]]


@pytest.fixture
async def setup_frontend(hass: HomeAssistant):
    assert await async_setup_component(hass, "frontend", {})
    await hass.async_block_till_done()


async def test_config_flow_and_single_instance(hass: HomeAssistant, setup_frontend) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"todo_entity": LIST, "name": "Küchen-Scanner"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Küchen-Scanner"
    assert result["data"] == {"todo_entity": LIST}
    assert result["options"]["duplicate_mode"] == "increment"
    assert result["options"]["notify_services"] == []
    assert result["options"]["notify_only_home"] is False
    assert result["options"]["notify_on_success"] is False
    assert result["options"]["quick_lists"] == []
    assert "notify_service" not in result["options"]
    await hass.async_block_till_done()

    again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert again["type"] is FlowResultType.ABORT


async def test_options_flow(hass: HomeAssistant, setup_frontend) -> None:
    async def _noop(call: ServiceCall):
        return None

    hass.services.async_register("notify", "mobile_app_handy", _noop)
    hass.services.async_register("notify", "mobile_app_pixel_frau", _noop)

    entry = MockConfigEntry(domain=DOMAIN, data={"todo_entity": LIST}, title="Barcode Scanner")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    user_input = {
        "todo_entity": "todo.picnic_warenkorb",
        "duplicate_mode": "skip",
        "unknown_mode": "add_ean_and_pending",
        "name_format": "product_only",
        "use_openfoodfacts": True,
        "use_eansuche": False,
        "notify_services": ["  notify.mobile_app_handy ", "notify.mobile_app_pixel_frau"],
        "notify_only_home": True,
        "notify_on_success": True,
        "quick_lists": ["todo.drogerie"],
        "opengtindb_query_id": "",
        "user_agent": "Test/1.0 (a@b.de)",
        "esphome_device_prefix": "",
    }
    bad = await hass.config_entries.options.async_configure(
        result["flow_id"], {**user_input, "notify_services": ["persistent_notification.create"]}
    )
    assert bad["type"] is FlowResultType.FORM and bad["errors"] == {"notify_services": "invalid_notify"}
    result = await hass.config_entries.options.async_configure(bad["flow_id"], user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["notify_services"] == ["notify.mobile_app_handy", "notify.mobile_app_pixel_frau"]
    assert entry.options["notify_only_home"] is True
    assert entry.options["notify_on_success"] is True
    assert entry.options["quick_lists"] == ["todo.drogerie"]
    assert "notify_service" not in entry.options
    manager = hass.data[DOMAIN]
    assert manager.todo_entity == "todo.picnic_warenkorb"
    assert manager.lookup.use_eansuche is False


async def test_full_flow(hass: HomeAssistant, setup_frontend, hass_ws_client) -> None:
    todo = TodoFake(hass)
    feedback: list[dict] = []

    async def show_result(call: ServiceCall):
        feedback.append(dict(call.data))

    hass.services.async_register("esphome", "barcode_scanner_show_result", show_result)

    entry = MockConfigEntry(domain=DOMAIN, data={"todo_entity": LIST}, title="Barcode Scanner")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Panel, Sensoren, Services
    assert PANEL_URL_PATH in hass.data[frontend.DATA_PANELS]
    panel = hass.data[frontend.DATA_PANELS][PANEL_URL_PATH]
    assert panel.sidebar_title == "Barcode Scanner" and panel.sidebar_icon == "mdi:barcode-scan"
    for sid in ("last_scan", "known_products", "unknown_count"):
        assert hass.states.get(f"sensor.shys_barcode_scanner_{sid}") is not None, sid
    for svc in ("scan", "learn_product", "delete_product", "dismiss_pending", "export_database", "import_database"):
        assert hass.services.has_service(DOMAIN, svc)

    manager = hass.data[DOMAIN]
    manager.lookup.async_lookup = AsyncMock(
        return_value=LookupResult(ean=EAN, found=True, name="Butter Streichgut gesalzen", brand="ja!", source="openfoodfacts")
    )

    # Event wie vom ESP
    hass.bus.async_fire("esphome.barcode_scanned", {"ean": EAN, "device": "barcode-scanner"})
    await hass.async_block_till_done()
    assert todo.summaries() == ["ja! Butter Streichgut gesalzen"]
    assert feedback[-1] == {"status": "ok", "ean": EAN, "brand": "ja!", "product": "Butter Streichgut gesalzen"}
    state = hass.states.get("sensor.shys_barcode_scanner_last_scan")
    assert state.state == "ja! Butter Streichgut gesalzen"
    assert state.attributes["source"] == "openfoodfacts" and state.attributes["status"] == "ok"
    assert hass.states.get("sensor.shys_barcode_scanner_known_products").state == "1"

    # Zweiter Scan: lokal, hochzählen
    manager.lookup.async_lookup.reset_mock()
    hass.bus.async_fire("esphome.barcode_scanned", {"ean": EAN, "device": "barcode-scanner"})
    await hass.async_block_till_done()
    manager.lookup.async_lookup.assert_not_called()
    assert todo.summaries() == ["2x ja! Butter Streichgut gesalzen"]

    # Unbekannter eigener Code
    resp = await hass.services.async_call(DOMAIN, "scan", {"ean": "QR-BROETCHEN"}, blocking=True, return_response=True)
    assert resp["status"] == "unknown"
    unknown = hass.states.get("sensor.shys_barcode_scanner_unknown_count")
    assert unknown.state == "1" and unknown.attributes["pending"] == ["QR-BROETCHEN"]

    # WebSocket wie das Panel
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/get_state"})
    msg = await ws.receive_json()
    assert msg["success"] and [p["ean"] for p in msg["result"]["pending"]] == ["QR-BROETCHEN"]

    await ws.send_json({"id": 2, "type": f"{DOMAIN}/learn", "ean": "QR-BROETCHEN", "name": "Brötchen", "brand": None, "target_list": None, "add_to_list": True})
    msg = await ws.receive_json()
    assert msg["success"] and msg["result"]["scan"]["status"] == "ok"
    assert todo.summaries()[-1] == "Brötchen"
    assert hass.states.get("sensor.shys_barcode_scanner_unknown_count").state == "0"

    await ws.send_json({"id": 3, "type": f"{DOMAIN}/learn", "ean": "X", "name": "Y", "target_list": "light.kueche"})
    msg = await ws.receive_json()
    assert not msg["success"]

    await ws.send_json({"id": 4, "type": f"{DOMAIN}/scan", "ean": "UNBEKANNT-2"})
    msg = await ws.receive_json()
    assert msg["result"]["status"] == "unknown"
    await ws.send_json({"id": 5, "type": f"{DOMAIN}/dismiss_pending", "ean": "UNBEKANNT-2"})
    assert (await ws.receive_json())["result"] == {"removed": True}

    await ws.send_json({"id": 6, "type": f"{DOMAIN}/export"})
    export = (await ws.receive_json())["result"]
    assert set(export["products"]) == {EAN, "QR-BROETCHEN"}

    await ws.send_json({"id": 7, "type": f"{DOMAIN}/delete", "ean": "QR-BROETCHEN"})
    assert (await ws.receive_json())["result"] == {"removed": True}

    await ws.send_json({"id": 8, "type": f"{DOMAIN}/import", "data": export, "merge": True})
    msg = await ws.receive_json()
    assert msg["success"] and msg["result"]["total"] == 2

    await ws.send_json({"id": 9, "type": f"{DOMAIN}/import", "data": "{kaputt", "merge": True})
    assert not (await ws.receive_json())["success"]

    # Services: export/import
    resp = await hass.services.async_call(DOMAIN, "export_database", {}, blocking=True, return_response=True)
    assert resp["format"] == "shys_barcode_scanner"
    resp = await hass.services.async_call(
        DOMAIN, "import_database", {"data": '{"products": {"1234": {"name": "Neu"}}}', "merge": True},
        blocking=True, return_response=True,
    )
    assert resp["imported"] == 1
    await hass.services.async_call(DOMAIN, "learn_product", {"ean": "555", "name": "Kaffee", "brand": "Tchibo"}, blocking=True)
    await hass.services.async_call(DOMAIN, "delete_product", {"ean": "555"}, blocking=True)
    assert manager.storage.get_product("555") is None

    # Persistenz im echten HA-Store
    await hass.async_block_till_done()
    from homeassistant.helpers.storage import Store

    stored = await Store(hass, 1, DOMAIN).async_load()
    assert EAN in stored["products"]

    # Entladen entfernt Panel und Listener
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert PANEL_URL_PATH not in hass.data[frontend.DATA_PANELS]
    assert DOMAIN not in hass.data

    # Neu laden geht (statischer Pfad nur einmal registriert)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert PANEL_URL_PATH in hass.data[frontend.DATA_PANELS]
    assert hass.data[DOMAIN].storage.get_product(EAN) is not None


async def test_panel_js_served(hass: HomeAssistant, setup_frontend, hass_client) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"todo_entity": LIST}, title="Barcode Scanner")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_client()
    resp = await client.get("/shys_barcode_scanner_static/shys-barcode-panel.js")
    assert resp.status == 200
    assert "shys-barcode-panel" in await resp.text()


async def test_with_real_shopping_list_todo(hass: HomeAssistant, setup_frontend, request) -> None:
    """Echte HA-Einkaufsliste (todo.shopping_list): add, get_items, update_item (2x/3x), skip."""
    # Die Einkaufsliste speichert in <config>/.shopping_list.json -> vorher/nachher leeren
    from pathlib import Path

    list_file = Path(hass.config.path(".shopping_list.json"))
    list_file.unlink(missing_ok=True)
    request.addfinalizer(lambda: list_file.unlink(missing_ok=True))
    sl = MockConfigEntry(domain="shopping_list")
    sl.add_to_hass(hass)
    assert await hass.config_entries.async_setup(sl.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(LIST) is not None

    entry = MockConfigEntry(domain=DOMAIN, data={"todo_entity": LIST}, title="Barcode Scanner")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    manager = hass.data[DOMAIN]
    await manager.async_learn(EAN, "Butter", brand="ja!")

    async def items() -> list[str]:
        resp = await hass.services.async_call(
            "todo", "get_items", {"status": ["needs_action"]}, target={"entity_id": LIST},
            blocking=True, return_response=True,
        )
        return [i["summary"] for i in resp[LIST]["items"]]

    for _ in range(3):
        hass.bus.async_fire("esphome.barcode_scanned", {"ean": EAN})
        await hass.async_block_till_done()
    assert await items() == ["3x ja! Butter"]

    hass.config_entries.async_update_entry(entry, options={**entry.options, "duplicate_mode": "skip"})
    await hass.async_block_till_done()
    res = await hass.services.async_call(DOMAIN, "scan", {"ean": EAN}, blocking=True, return_response=True)
    assert res["status"] == "duplicate"
    assert await items() == ["3x ja! Butter"]

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "duplicate_mode": "allow", "unknown_mode": "add_ean_and_pending"}
    )
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "scan", {"ean": EAN}, blocking=True)
    await hass.services.async_call(DOMAIN, "scan", {"ean": "MEIN-QR"}, blocking=True)
    assert await items() == ["3x ja! Butter", "ja! Butter", "MEIN-QR"]


async def test_push_reply_on_real_event_bus(hass: HomeAssistant, setup_frontend, request) -> None:
    """v1.1.0: Push-Antwort über den echten Event-Bus -> gelernt + auf todo.shopping_list."""
    from pathlib import Path

    list_file = Path(hass.config.path(".shopping_list.json"))
    list_file.unlink(missing_ok=True)
    request.addfinalizer(lambda: list_file.unlink(missing_ok=True))
    sl = MockConfigEntry(domain="shopping_list")
    sl.add_to_hass(hass)
    assert await hass.config_entries.async_setup(sl.entry_id)
    await hass.async_block_till_done()

    sent: list[dict] = []

    async def mobile_app_handy(call: ServiceCall):
        sent.append(dict(call.data))

    hass.services.async_register("notify", "mobile_app_handy", mobile_app_handy)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"todo_entity": LIST},
        options={"notify_service": "notify.mobile_app_handy"},
        title="Barcode Scanner",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    manager = hass.data[DOMAIN]
    manager.lookup.async_lookup = AsyncMock(return_value=LookupResult(ean=EAN, attempts={"openfoodfacts": "not_found"}))

    async def items() -> list[str]:
        resp = await hass.services.async_call(
            "todo", "get_items", {"status": ["needs_action"]}, target={"entity_id": LIST},
            blocking=True, return_response=True,
        )
        return [i["summary"] for i in resp[LIST]["items"]]

    hass.bus.async_fire("esphome.barcode_scanned", {"ean": EAN})
    await hass.async_block_till_done()
    assert len(sent) == 1
    payload = sent[0]
    assert payload["data"]["tag"] == f"shys_barcode_{EAN}"
    assert payload["data"]["clickAction"] == f"/{PANEL_URL_PATH}" == payload["data"]["url"]
    assert [a["action"] for a in payload["data"]["actions"]] == [f"SHYS_REPLY_{EAN}", f"SHYS_DISMISS_{EAN}"]
    assert manager.last_result["status"] == "unknown" and manager.last_result["name"] == ""
    assert hass.states.get("sensor.shys_barcode_scanner_last_scan").state == EAN

    # Fremde REPLY-Aktion einer anderen Automation -> ignoriert
    hass.bus.async_fire("mobile_app_notification_action", {"action": "REPLY", "tag": "other_automation", "reply_text": "Hallo"})
    await hass.async_block_till_done()
    assert manager.storage.get_product(EAN) is None and await items() == []

    # Android-Antwort (generisches REPLY + tag, wie es die App zurückschickt)
    hass.bus.async_fire(
        "mobile_app_notification_action",
        {"action": "REPLY", "tag": f"shys_barcode_{EAN}", "reply_text": "Hafermilch"},
    )
    await hass.async_block_till_done()
    assert manager.storage.get_product(EAN)["name"] == "Hafermilch"
    assert EAN not in manager.storage.pending
    assert await items() == ["Hafermilch"]
    assert sent[-1] == {"message": "clear_notification", "data": {"tag": f"shys_barcode_{EAN}"}}
    assert hass.states.get("sensor.shys_barcode_scanner_unknown_count").state == "0"
    assert manager.last_result["status"] == "ok"
    assert manager.last_result["name"] == "Hafermilch"
    assert hass.states.get("sensor.shys_barcode_scanner_last_scan").state == "Hafermilch"

    # iOS-Antwort auf zweiten Code (eigene Aktions-ID, kein tag)
    await hass.services.async_call(DOMAIN, "scan", {"ean": "QR-HAFER"}, blocking=True)
    hass.bus.async_fire("mobile_app_notification_action", {"action": "SHYS_REPLY_QR-HAFER", "reply_text": "Haferflocken"})
    await hass.async_block_till_done()
    assert await items() == ["Hafermilch", "Haferflocken"]

    # Verwerfen
    await hass.services.async_call(DOMAIN, "scan", {"ean": "QR-WEG"}, blocking=True)
    assert "QR-WEG" in manager.storage.pending
    hass.bus.async_fire("mobile_app_notification_action", {"action": "SHYS_DISMISS_QR-WEG"})
    await hass.async_block_till_done()
    assert "QR-WEG" not in manager.storage.pending

    # Entladen entfernt den Listener
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await manager.storage.add_pending("QR-NACH-UNLOAD")
    hass.bus.async_fire("mobile_app_notification_action", {"action": "SHYS_REPLY_QR-NACH-UNLOAD", "reply_text": "X"})
    await hass.async_block_till_done()
    assert manager.storage.get_product("QR-NACH-UNLOAD") is None


async def test_legacy_notify_service_migrates_in_options_and_runtime(
    hass: HomeAssistant, setup_frontend
) -> None:
    """Bestehende Configs mit notify_service funktionieren und werden beim Speichern migriert."""
    sent: list[tuple[str, dict]] = []

    async def record(call: ServiceCall):
        sent.append((call.service, dict(call.data)))

    hass.services.async_register("notify", "mobile_app_handy", record)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"todo_entity": LIST},
        options={"notify_service": "notify.mobile_app_handy"},
        title="Barcode Scanner",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    manager = hass.data[DOMAIN]
    manager.lookup.async_lookup = AsyncMock(return_value=LookupResult(ean=EAN, attempts={"openfoodfacts": "not_found"}))
    TodoFake(hass)
    await hass.services.async_call(DOMAIN, "scan", {"ean": EAN}, blocking=True)
    assert sent and sent[0][0] == "mobile_app_handy"
    assert sent[0][1]["data"]["tag"] == f"shys_barcode_{EAN}"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    user_input = {
        "todo_entity": LIST,
        "duplicate_mode": "increment",
        "unknown_mode": "pending_only",
        "name_format": "brand_product",
        "use_openfoodfacts": True,
        "use_eansuche": True,
        "notify_services": ["notify.mobile_app_handy"],
        "notify_only_home": False,
        "notify_on_success": False,
        "quick_lists": [],
        "opengtindb_query_id": "",
        "user_agent": "",
        "esphome_device_prefix": "",
    }
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["notify_services"] == ["notify.mobile_app_handy"]
    assert "notify_service" not in entry.options


async def test_multi_phone_presence_and_clear_on_panel(
    hass: HomeAssistant, setup_frontend, hass_ws_client
) -> None:
    """Mehrere Handys, Anwesenheitsfilter, clear auf allen Geräten inkl. Panel."""
    sent: list[tuple[str, dict]] = []

    async def record(call: ServiceCall):
        sent.append((call.service, dict(call.data)))

    hass.services.async_register("notify", "mobile_app_handy", record)
    hass.services.async_register("notify", "mobile_app_pixel_frau", record)
    hass.services.async_register("notify", "familie", record)
    TodoFake(hass)

    hass.states.async_set("device_tracker.handy", "home")
    hass.states.async_set("person.kalle", "home", {"device_trackers": ["device_tracker.handy"]})
    hass.states.async_set("device_tracker.pixel_frau", "Arbeit")
    hass.states.async_set("person.anna", "Arbeit", {"device_trackers": ["device_tracker.pixel_frau"]})

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"todo_entity": LIST},
        options={
            "notify_services": [
                "notify.mobile_app_handy",
                "notify.mobile_app_pixel_frau",
                "notify.familie",
            ],
            "notify_only_home": True,
        },
        title="Barcode Scanner",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    manager = hass.data[DOMAIN]
    manager.lookup.async_lookup = AsyncMock(return_value=LookupResult(ean=EAN, attempts={"openfoodfacts": "not_found"}))

    await hass.services.async_call(DOMAIN, "scan", {"ean": EAN}, blocking=True)
    unknown = [c for c in sent if c[1].get("message") != "clear_notification"]
    assert {c[0] for c in unknown} == {"mobile_app_handy", "familie"}  # Frau nicht zuhause, Gruppe fail-open

    # Panel: anlernen räumt auf ALLEN konfigurierten Handys auf
    client = await hass_ws_client(hass)
    await client.send_json(
        {"id": 40, "type": f"{DOMAIN}/learn", "ean": EAN, "name": "Hafermilch", "add_to_list": True}
    )
    msg = await client.receive_json()
    assert msg["success"] is True
    clears = [c for c in sent if c[1].get("message") == "clear_notification"]
    assert {c[0] for c in clears} == {"mobile_app_handy", "mobile_app_pixel_frau", "familie"}
    assert all(c[1]["data"]["tag"] == f"shys_barcode_{EAN}" for c in clears)

    # Panel-Verwerfen räumt ebenfalls auf
    sent.clear()
    await hass.services.async_call(DOMAIN, "scan", {"ean": "QR-WEG"}, blocking=True)
    await client.send_json({"id": 41, "type": f"{DOMAIN}/dismiss_pending", "ean": "QR-WEG"})
    msg = await client.receive_json()
    assert msg["success"] is True and msg["result"]["removed"] is True
    clears = [c for c in sent if c[1].get("message") == "clear_notification"]
    assert {c[0] for c in clears} == {"mobile_app_handy", "mobile_app_pixel_frau", "familie"}
    assert all(c[1]["data"]["tag"] == "shys_barcode_QR-WEG" for c in clears)


async def test_device_tracker_fallback_without_person(hass: HomeAssistant, setup_frontend) -> None:
    sent: list[tuple[str, dict]] = []

    async def record(call: ServiceCall):
        sent.append((call.service, dict(call.data)))

    hass.services.async_register("notify", "mobile_app_handy", record)
    TodoFake(hass)
    hass.states.async_set("device_tracker.handy", "not_home")

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"todo_entity": LIST},
        options={"notify_services": ["notify.mobile_app_handy"], "notify_only_home": True},
        title="Barcode Scanner",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    manager = hass.data[DOMAIN]
    manager.lookup.async_lookup = AsyncMock(return_value=LookupResult(ean=EAN, attempts={"openfoodfacts": "not_found"}))
    await hass.services.async_call(DOMAIN, "scan", {"ean": EAN}, blocking=True)
    assert sent == []

    hass.states.async_set("device_tracker.handy", "home")
    await hass.services.async_call(DOMAIN, "scan", {"ean": "QR-HOME"}, blocking=True)
    assert [c[0] for c in sent if c[1].get("message") != "clear_notification"] == ["mobile_app_handy"]


async def test_undo_add_increment_and_websocket(hass: HomeAssistant, setup_frontend, hass_ws_client) -> None:
    todo = TodoFake(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={"todo_entity": LIST}, title="Barcode Scanner")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    manager = hass.data[DOMAIN]
    manager.lookup.async_lookup = AsyncMock(
        return_value=LookupResult(ean=EAN, found=True, name="Milch", brand="", source="openfoodfacts")
    )

    first = await hass.services.async_call(DOMAIN, "scan", {"ean": EAN}, blocking=True, return_response=True)
    second = await hass.services.async_call(DOMAIN, "scan", {"ean": EAN}, blocking=True, return_response=True)
    assert todo.summaries() == ["2x Milch"]
    assert first["action"] == "added" and second["action"] == "incremented"
    assert first["undo_id"] and second["undo_id"]

    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/get_state"})
    state = (await ws.receive_json())["result"]
    assert state["last_undoable_scan"]["undo_id"] == second["undo_id"]
    assert state["last_undoable_scan"]["action_kind"] == "incremented"

    await ws.send_json({"id": 2, "type": f"{DOMAIN}/undo_scan", "undo_id": second["undo_id"]})
    msg = await ws.receive_json()
    assert msg["success"] is True and msg["result"]["ok"] is True
    assert todo.summaries() == ["Milch"]

    await ws.send_json({"id": 3, "type": f"{DOMAIN}/undo_scan"})
    msg = await ws.receive_json()
    assert msg["success"] is True and msg["result"]["ok"] is True
    assert todo.summaries() == []


async def test_undo_push_button_and_idempotent(hass: HomeAssistant, setup_frontend) -> None:
    sent: list[tuple[str, dict]] = []

    async def record(call: ServiceCall):
        sent.append((call.service, dict(call.data)))

    hass.services.async_register("notify", "mobile_app_handy", record)
    hass.states.async_set(LIST, "0", {"friendly_name": "Einkaufsliste"})
    todo = TodoFake(hass)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"todo_entity": LIST},
        options={"notify_services": ["notify.mobile_app_handy"], "notify_on_success": True},
        title="Barcode Scanner",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    manager = hass.data[DOMAIN]
    await manager.storage.upsert_product(EAN, "Milch")
    result = await hass.services.async_call(DOMAIN, "scan", {"ean": EAN}, blocking=True, return_response=True)
    undo_id = result["undo_id"]
    success = [c for c in sent if c[1].get("title") == "Auf der Einkaufsliste"]
    assert len(success) == 1
    assert success[0][1]["data"]["tag"] == f"shys_barcode_ok_{undo_id}"
    assert success[0][1]["data"]["actions"][0]["action"] == f"SHYS_UNDO_{undo_id}"

    hass.bus.async_fire("mobile_app_notification_action", {"action": f"SHYS_UNDO_{undo_id}"})
    await hass.async_block_till_done()
    assert todo.summaries() == []
    clears = [c for c in sent if c[1].get("message") == "clear_notification"]
    assert any(c[1]["data"]["tag"] == f"shys_barcode_ok_{undo_id}" for c in clears)

    hass.bus.async_fire("mobile_app_notification_action", {"action": f"SHYS_UNDO_{undo_id}"})
    await hass.async_block_till_done()
    assert todo.summaries() == []


async def test_notify_on_success_off_and_only_home(hass: HomeAssistant, setup_frontend) -> None:
    sent: list[tuple[str, dict]] = []

    async def record(call: ServiceCall):
        sent.append((call.service, dict(call.data)))

    hass.services.async_register("notify", "mobile_app_handy", record)
    hass.services.async_register("notify", "mobile_app_pixel_frau", record)
    TodoFake(hass)
    hass.states.async_set(LIST, "0", {"friendly_name": "Einkaufsliste"})
    hass.states.async_set("device_tracker.handy", "home")
    hass.states.async_set("person.kalle", "home", {"device_trackers": ["device_tracker.handy"]})
    hass.states.async_set("device_tracker.pixel_frau", "not_home")
    hass.states.async_set("person.anna", "not_home", {"device_trackers": ["device_tracker.pixel_frau"]})

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"todo_entity": LIST},
        options={"notify_services": ["notify.mobile_app_handy", "notify.mobile_app_pixel_frau"]},
        title="Barcode Scanner",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    manager = hass.data[DOMAIN]
    await manager.storage.upsert_product(EAN, "Milch")
    await hass.services.async_call(DOMAIN, "scan", {"ean": EAN}, blocking=True)
    assert sent == []

    manager.update_config(
        {
            **manager.config,
            "notify_on_success": True,
            "notify_only_home": True,
        }
    )
    other = "4000417025005"
    await manager.storage.upsert_product(other, "Butter")
    await hass.services.async_call(DOMAIN, "scan", {"ean": other}, blocking=True)
    success = [c for c in sent if c[1].get("title") == "Auf der Einkaufsliste"]
    assert [c[0] for c in success] == ["mobile_app_handy"]


async def test_quick_lists_push_reply_and_at_suffix(hass: HomeAssistant, setup_frontend) -> None:
    sent: list[dict] = []

    async def record(call: ServiceCall):
        sent.append(dict(call.data))

    hass.services.async_register("notify", "mobile_app_handy", record)
    todo = TodoFake(hass)
    todo.items["todo.drogerie"] = []
    hass.states.async_set(LIST, "0", {"friendly_name": "Einkaufsliste"})
    hass.states.async_set("todo.drogerie", "0", {"friendly_name": "Drogerie"})

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"todo_entity": LIST},
        options={
            "notify_services": ["notify.mobile_app_handy"],
            "quick_lists": ["todo.drogerie"],
        },
        title="Barcode Scanner",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    manager = hass.data[DOMAIN]
    manager.lookup.async_lookup = AsyncMock(return_value=LookupResult(ean=EAN, attempts={"openfoodfacts": "not_found"}))

    # Fall B: zwei Listen-Buttons + Verwerfen
    await hass.services.async_call(DOMAIN, "scan", {"ean": EAN}, blocking=True)
    payload = sent[0]
    assert [a["action"] for a in payload["data"]["actions"]] == [
        f"SHYS_REPLY_L0_{EAN}",
        f"SHYS_REPLY_L1_{EAN}",
        f"SHYS_DISMISS_{EAN}",
    ]
    assert payload["data"]["actions"][0]["title"] == "→ Einkaufsliste"
    assert payload["data"]["actions"][1]["title"] == "→ Drogerie"

    hass.bus.async_fire(
        "mobile_app_notification_action",
        {"action": f"SHYS_REPLY_L1_{EAN}", "reply_text": "Duschgel"},
    )
    await hass.async_block_till_done()
    assert manager.storage.get_product(EAN)["name"] == "Duschgel"
    assert manager.storage.get_product(EAN)["target_list"] == "todo.drogerie"
    assert todo.summaries("todo.drogerie") == ["Duschgel"]
    assert todo.summaries() == []

    # @Listenname auch ohne Listen-Buttons (hier nach bereits bekanntem Code ein neuer)
    await hass.services.async_call(DOMAIN, "scan", {"ean": "QR-SPUEL"}, blocking=True)
    hass.bus.async_fire(
        "mobile_app_notification_action",
        {"action": "SHYS_REPLY_QR-SPUEL", "reply_text": "Spülmittel @Drogerie"},
    )
    await hass.async_block_till_done()
    assert manager.storage.get_product("QR-SPUEL")["name"] == "Spülmittel"
    assert manager.storage.get_product("QR-SPUEL")["target_list"] == "todo.drogerie"
    assert todo.summaries("todo.drogerie") == ["Duschgel", "Spülmittel"]


async def test_settings_get_save_and_test_push(hass: HomeAssistant, setup_frontend, hass_ws_client) -> None:
    sent: list[tuple[str, dict]] = []

    async def record(call: ServiceCall):
        sent.append((call.service, dict(call.data)))

    async def failing(call: ServiceCall):
        raise HomeAssistantError("offline")

    hass.services.async_register("notify", "mobile_app_handy", record)
    hass.services.async_register("notify", "mobile_app_pixel_frau", failing)
    hass.states.async_set(LIST, "0", {"friendly_name": "Einkaufsliste"})
    hass.states.async_set("todo.drogerie", "0", {"friendly_name": "Drogerie"})
    TodoFake(hass)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"todo_entity": LIST},
        options={"notify_service": "notify.mobile_app_handy"},
        title="Barcode Scanner",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/get_settings"})
    msg = await ws.receive_json()
    assert msg["success"] is True
    settings = msg["result"]["settings"]
    assert settings["todo_entity"] == LIST
    assert settings["notify_services"] == ["notify.mobile_app_handy"]
    assert "notify_service" not in settings
    todo_names = {item["entity_id"]: item["name"] for item in msg["result"]["available_todo_lists"]}
    assert todo_names[LIST] == "Einkaufsliste" and todo_names["todo.drogerie"] == "Drogerie"
    assert "notify.mobile_app_handy" in msg["result"]["available_notify_services"]
    assert "notify.mobile_app_pixel_frau" in msg["result"]["available_notify_services"]

    await ws.send_json(
        {
            "id": 2,
            "type": f"{DOMAIN}/save_settings",
            "settings": {
                "todo_entity": LIST,
                "quick_lists": ["todo.drogerie"],
                "duplicate_mode": "skip",
                "name_format": "product_only",
                "notify_services": ["notify.mobile_app_handy", "notify.mobile_app_pixel_frau"],
                "notify_only_home": True,
                "notify_on_success": True,
                "use_openfoodfacts": False,
                "use_eansuche": True,
                "opengtindb_query_id": "",
                "user_agent": "Test/1.0 (a@b.de)",
                "esphome_device_prefix": "barcode-scanner",
            },
        }
    )
    msg = await ws.receive_json()
    assert msg["success"] is True
    saved = msg["result"]["settings"]
    await hass.async_block_till_done()
    assert entry.options["quick_lists"] == ["todo.drogerie"]
    assert entry.options["duplicate_mode"] == "skip"
    assert entry.options["notify_services"] == ["notify.mobile_app_handy", "notify.mobile_app_pixel_frau"]
    assert entry.options["notify_only_home"] is True
    assert entry.options["notify_on_success"] is True
    assert entry.options["use_openfoodfacts"] is False
    assert entry.options["esphome_device_prefix"] == "barcode-scanner"
    assert "notify_service" not in entry.options
    assert saved == entry.options or saved["notify_services"] == entry.options["notify_services"]
    manager = hass.data[DOMAIN]
    assert manager.todo_entity == LIST
    assert manager.config["duplicate_mode"] == "skip"
    assert manager.config["notify_on_success"] is True
    assert manager.lookup.use_openfoodfacts is False

    await ws.send_json({"id": 3, "type": f"{DOMAIN}/save_settings", "settings": {"todo_entity": "light.kueche"}})
    msg = await ws.receive_json()
    assert msg["success"] is False

    # Gespeicherte Dienste: Teilerfolg (ein Handy offline)
    await ws.send_json({"id": 4, "type": f"{DOMAIN}/test_push"})
    msg = await ws.receive_json()
    assert msg["success"] is True
    result = msg["result"]
    assert result["ok"] is False
    assert result["sent"] == ["notify.mobile_app_handy"]
    assert result["errors"][0]["service"] == "notify.mobile_app_pixel_frau"
    assert sent[0][1]["data"]["tag"] == "shys_barcode_test"
    assert sent[0][1]["title"] == "SHYS Barcode Scanner – Test"

    # Vor dem Speichern: nur das funktionierende Handy testen
    sent.clear()
    await ws.send_json({"id": 5, "type": f"{DOMAIN}/test_push", "notify_services": ["notify.mobile_app_handy"]})
    msg = await ws.receive_json()
    assert msg["success"] is True and msg["result"]["ok"] is True
    assert msg["result"]["sent"] == ["notify.mobile_app_handy"]
    assert sent and sent[0][1]["message"] == "Push-Benachrichtigungen sind richtig eingerichtet."


async def test_settings_and_test_push_require_admin(
    hass: HomeAssistant, setup_frontend, hass_ws_client, hass_read_only_access_token
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"todo_entity": LIST}, title="Barcode Scanner")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json({"id": 1, "type": f"{DOMAIN}/get_settings"})
    msg = await client.receive_json()
    assert msg["success"] is True

    await client.send_json({"id": 2, "type": f"{DOMAIN}/save_settings", "settings": {"notify_on_success": True}})
    msg = await client.receive_json()
    assert msg["success"] is False
    assert entry.options.get("notify_on_success") is not True

    await client.send_json({"id": 3, "type": f"{DOMAIN}/test_push"})
    msg = await client.receive_json()
    assert msg["success"] is False

    admin = await hass_ws_client(hass)
    await admin.send_json({"id": 4, "type": f"{DOMAIN}/save_settings", "settings": {"notify_on_success": True}})
    msg = await admin.receive_json()
    assert msg["success"] is True
    assert entry.options["notify_on_success"] is True


# ------------------------------------------------------------------ Offline-Katalog (v1.4.0)


class _FakeContent:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def iter_chunked(self, size: int):
        for i in range(0, len(self._data), size):
            yield self._data[i : i + size]


class _FakeResp:
    def __init__(self, status: int, data: bytes) -> None:
        self.status = status
        self._data = data
        self.content = _FakeContent(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._data.decode("utf-8")


class _FakeCatalogSession:
    """Liefert Manifest + .tsv.gz wie das GitHub-Release offline-catalog-de."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.urls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        name = url.rsplit("/", 1)[-1]
        if name in self.files:
            return _FakeResp(200, self.files[name])
        return _FakeResp(404, b"")


def _catalog_files() -> dict[str, bytes]:
    import gzip
    import hashlib
    import json

    def tsv(rows):
        body = "ean\tbrand\tname\n" + "".join("\t".join(r) + "\n" for r in rows)
        return gzip.compress(body.encode("utf-8"), mtime=0)

    files = {
        "catalog-de-food.tsv.gz": tsv([("4001686009727", "Haribo", "Stars"), (EAN, "ja!", "Butter")]),
        "catalog-de-beauty.tsv.gz": tsv([("4005900036650", "Odol-med3", "Zahnpasta Extra White")]),
    }
    manifest = {
        "schema_version": 1,
        "generated_at": "2026-12-01T03:00:00Z",
        "license": "ODbL (Open Database License)",
        "categories": {
            name.removeprefix("catalog-de-").removesuffix(".tsv.gz"): {
                "file": name, "count": 1, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            }
            for name, data in files.items()
        },
    }
    files["catalog-de-manifest.json"] = json.dumps(manifest).encode()
    return files


async def test_offline_catalog_websocket_and_scan(
    hass: HomeAssistant, setup_frontend, hass_ws_client, hass_read_only_access_token
) -> None:
    import os

    todo = TodoFake(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={"todo_entity": LIST}, title="Barcode Scanner")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    manager = hass.data[DOMAIN]
    catalog = manager.catalog
    assert catalog.path == hass.config.path("shys_barcode_scanner_catalog.db")
    assert ".storage" not in catalog.path
    fake = _FakeCatalogSession(_catalog_files())
    catalog._session = fake
    manager.lookup.async_lookup = AsyncMock(return_value=LookupResult(ean=EAN, attempts={}))

    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/catalog_status"})
    msg = await ws.receive_json()
    assert msg["success"] and msg["result"]["installed"] is False and msg["result"]["enabled"] is True
    assert "ODbL" in msg["result"]["license"]

    await ws.send_json({"id": 2, "type": f"{DOMAIN}/catalog_check_updates"})
    msg = await ws.receive_json()
    assert msg["success"] and [c["key"] for c in msg["result"]["categories"]] == ["food", "beauty"]

    # Nur-Lese-Benutzer darf nicht laden/löschen
    ro = await hass_ws_client(hass, hass_read_only_access_token)
    await ro.send_json({"id": 3, "type": f"{DOMAIN}/catalog_download", "categories": ["food"]})
    msg = await ro.receive_json()
    assert msg["success"] is False
    await ro.send_json({"id": 4, "type": f"{DOMAIN}/catalog_delete"})
    assert (await ro.receive_json())["success"] is False

    await ws.send_json({"id": 5, "type": f"{DOMAIN}/catalog_download", "categories": ["elektronik"]})
    assert (await ws.receive_json())["success"] is False

    await ws.send_json({"id": 6, "type": f"{DOMAIN}/catalog_download", "categories": ["food", "beauty"]})
    msg = await ws.receive_json()
    assert msg["success"], msg
    assert msg["result"]["installed"] is True and msg["result"]["total_count"] == 3
    assert msg["result"]["installed_categories"] == ["food", "beauty"]
    assert os.path.isfile(catalog.path)

    # Scan über den echten Event-Bus: Offline-Treffer, keine Online-API
    hass.bus.async_fire("esphome.barcode_scanned", {"ean": "4001686009727"})
    await hass.async_block_till_done()
    assert todo.summaries() == ["Haribo Stars"]
    manager.lookup.async_lookup.assert_not_called()
    assert manager.storage.get_product("4001686009727")["source"] == "offline_catalog"
    assert len(manager.storage.list_products()) == 1

    await ws.send_json({"id": 7, "type": f"{DOMAIN}/catalog_search", "query": "zahnpasta"})
    msg = await ws.receive_json()
    assert msg["success"] and [r["ean"] for r in msg["result"]["results"]] == ["4005900036650"]
    assert msg["result"]["results"][0]["in_own_db"] is False
    await ro.send_json({"id": 8, "type": f"{DOMAIN}/catalog_search", "query": "Haribo", "category": "food"})
    msg = await ro.receive_json()
    assert msg["success"] and msg["result"]["results"][0]["in_own_db"] is True

    await ws.send_json({"id": 9, "type": f"{DOMAIN}/catalog_add", "ean": "4005900036650", "add_to_list": True})
    msg = await ws.receive_json()
    assert msg["success"] and msg["result"]["product"]["source"] == "offline_catalog"
    assert todo.summaries() == ["Haribo Stars", "Odol-med3 Zahnpasta Extra White"]

    # Offline-Katalog per Einstellungen abschalten -> Online-Kaskade
    await ws.send_json({"id": 10, "type": f"{DOMAIN}/save_settings", "settings": {"use_offline_catalog": False}})
    msg = await ws.receive_json()
    assert msg["success"] and entry.options["use_offline_catalog"] is False
    assert manager.offline_catalog_enabled is False
    # update_config baut die Lookup-Kaskade neu -> erneut mocken (kein echtes Internet im Test)
    manager.lookup.async_lookup = AsyncMock(return_value=LookupResult(ean=EAN, attempts={}))
    hass.bus.async_fire("esphome.barcode_scanned", {"ean": EAN})
    await hass.async_block_till_done()
    manager.lookup.async_lookup.assert_awaited_once()

    await ws.send_json({"id": 11, "type": f"{DOMAIN}/catalog_delete"})
    msg = await ws.receive_json()
    assert msg["success"] and msg["result"]["removed"] is True
    assert msg["result"]["status"]["installed"] is False
    assert not os.path.exists(catalog.path)

    assert await hass.config_entries.async_unload(entry.entry_id)
