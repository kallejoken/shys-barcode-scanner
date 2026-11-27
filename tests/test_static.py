"""Statische Prüfungen: JSON, Manifest, Übersetzungen, services.yaml, Python/JS-Syntax."""

from __future__ import annotations

import importlib
import json
import py_compile
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components" / "shys_barcode_scanner"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_hacs_json():
    data = load(ROOT / "hacs.json")
    assert data == {"name": "SHYS Barcode Scanner", "render_readme": True, "homeassistant": "2024.12.0"}


def test_manifest():
    m = load(COMP / "manifest.json")
    assert m["domain"] == "shys_barcode_scanner"
    assert m["name"] == "SHYS Barcode Scanner"
    assert m["version"] == "1.4.1"
    assert m["config_flow"] is True
    assert m["iot_class"] == "local_push"
    assert m["dependencies"] == ["http", "panel_custom", "websocket_api"]
    assert m["codeowners"] == ["@kallejoken", "@SmartHome-yourself"]
    keys = list(m)
    assert keys[:2] == ["domain", "name"] and keys[2:] == sorted(keys[2:]), "hassfest-Reihenfolge"
    from custom_components.shys_barcode_scanner.const import DOMAIN, VERSION

    assert DOMAIN == m["domain"] and VERSION == m["version"]


def _flatten(d, prefix=""):
    out = set()
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out |= _flatten(v, key + ".")
        else:
            out.add(key)
    return out


def test_translations_consistent():
    strings = load(COMP / "strings.json")
    en = load(COMP / "translations" / "en.json")
    de = load(COMP / "translations" / "de.json")
    assert strings == en
    assert _flatten(en) == _flatten(de)
    assert "„" in json.dumps(de, ensure_ascii=False)


def test_translations_cover_options_and_services():
    import yaml  # type: ignore[import-not-found]

    from custom_components.shys_barcode_scanner import const

    de = load(COMP / "translations" / "de.json")
    opt = de["options"]["step"]["init"]["data"]
    for key in (const.CONF_TODO_ENTITY, const.CONF_DUPLICATE_MODE, const.CONF_UNKNOWN_MODE,
                const.CONF_NAME_FORMAT, const.CONF_USE_OPENFOODFACTS, const.CONF_USE_EANSUCHE,
                const.CONF_OPENGTINDB_QUERY_ID, const.CONF_USER_AGENT, const.CONF_NOTIFY_SERVICES,
                const.CONF_NOTIFY_ONLY_HOME, const.CONF_NOTIFY_ON_SUCCESS, const.CONF_QUICK_LISTS,
                const.CONF_ESPHOME_DEVICE_PREFIX, const.CONF_USE_OFFLINE_CATALOG,
                const.CONF_CATALOG_BASE_URL):
        assert key in opt
    assert set(de["selector"]["duplicate_mode"]["options"]) == set(const.DUPLICATE_MODES)
    assert set(de["selector"]["unknown_mode"]["options"]) == set(const.UNKNOWN_MODES)
    assert set(de["selector"]["name_format"]["options"]) == set(const.NAME_FORMATS)
    assert set(de["entity"]["sensor"]) == {"last_scan", "known_products", "unknown_count"}

    services = yaml.safe_load((COMP / "services.yaml").read_text(encoding="utf-8"))
    assert set(services) == {"scan", "learn_product", "delete_product", "dismiss_pending",
                             "export_database", "import_database"}
    assert set(services) == set(de["services"])
    for name, spec in services.items():
        fields = (spec or {}).get("fields", {})
        assert set(fields) == set(de["services"][name].get("fields", {})), name


def test_python_syntax():
    for path in list(COMP.glob("*.py")) + list((ROOT / "tests").glob("*.py")):
        py_compile.compile(str(path), doraise=True)


@pytest.mark.parametrize("module", ["const", "storage", "lookup", "manager", "config_flow",
                                    "settings", "sensor", "websocket", "__init__"])
def test_modules_import(module):
    name = "custom_components.shys_barcode_scanner" + ("" if module == "__init__" else f".{module}")
    importlib.import_module(name)


def test_panel_js_syntax():
    js = COMP / "frontend" / "shys-barcode-panel.js"
    text = js.read_text(encoding="utf-8")
    assert 'customElements.define("shys-barcode-panel"' in text
    for label in ("Unbekannte Scans", "Produktdatenbank", "Test &amp; Backup", "Einstellungen",
                  "Speichern &amp; auf Liste", "Verwerfen", "+ Eigenen Code anlegen", "Rückgängig",
                  "Von der Liste entfernt", "Einstellungen speichern", "Test-Push senden"):
        assert label in text
    for ws in ("get_state", "learn", "delete", "dismiss_pending", "scan", "undo_scan",
               "get_settings", "save_settings", "test_push", "import", "export"):
        assert f'_ws("{ws}"' in text
    node = shutil.which("node")
    if node is None:
        pytest.skip("node nicht installiert")
    subprocess.run([node, "--check", str(js)], check=True)


def test_websocket_commands_match_panel():
    text = (COMP / "websocket.py").read_text(encoding="utf-8")
    for ws in ("get_state", "learn", "delete", "dismiss_pending", "scan", "undo_scan",
               "get_settings", "save_settings", "test_push", "import", "export"):
        assert f'f"{{DOMAIN}}/{ws}"' in text


def _png_size(path: Path) -> tuple[int, int]:
    import struct

    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path.name
    return struct.unpack(">II", data[16:24])


def test_brand_assets():
    """Lokale Markenbilder, die HA 2026.10.4 unter /api/brands/integration/<domain>/ ausliefert."""
    brand = COMP / "brand"
    for name in ("icon.png", "logo.png", "icon@2x.png", "logo@2x.png"):
        assert (brand / name).is_file(), name
    assert _png_size(brand / "icon.png") == (256, 256)
    assert _png_size(brand / "icon@2x.png") == (512, 512)
    logo_w, logo_h = _png_size(brand / "logo.png")
    logo2_w, logo2_h = _png_size(brand / "logo@2x.png")
    assert logo_w > logo_h and 128 <= logo_h <= 256
    assert logo2_w > logo2_h and 256 <= logo2_h <= 512
    assert (logo_w, logo_h) == (960, 256)
    assert (logo2_w, logo2_h) == (1920, 512)
