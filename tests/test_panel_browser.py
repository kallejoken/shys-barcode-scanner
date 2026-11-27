"""Panel im echten Headless-Chrome mit Attrappe für hass.callWS (wird übersprungen ohne Chrome)."""

from __future__ import annotations

import functools
import html
import http.server
import json
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHROME = next(
    (p for p in ("google-chrome", "chromium", "chromium-browser", "chrome") if shutil.which(p)), None
)


@pytest.mark.skipif(CHROME is None, reason="kein Chrome/Chromium installiert")
def test_panel_in_headless_chrome():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/tests/frontend/panel_harness.html"
        proc = subprocess.run(
            [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
             "--virtual-time-budget=5000", "--dump-dom", url],
            check=False, capture_output=True, text=True, timeout=90,
        )
    finally:
        server.shutdown()
    match = re.search(r'<pre id="result">(.*?)</pre>', proc.stdout, re.DOTALL)
    assert match, proc.stdout[-2000:] + proc.stderr[-2000:]
    out = json.loads(html.unescape(match.group(1)))
    assert out.get("ok"), out.get("error")
    assert out["tabs"][1:] == ["Produktdatenbank (1)", "Test & Backup", "Offline-Katalog", "Einstellungen"]
    assert out["tabs"][0].startswith("Unbekannte Scans")
    assert out["escaped"] is True
    assert out["noDuplicateEan"] is True
    assert out["lastScanBadge"] is True
    assert out["reloadQueued"] is True
    assert out["todoOptions"] == ["Standard (todo.shopping_list)", "Einkaufsliste", "Picnic Warenkorb"]
    assert out["learnCall"] == {
        "type": "shys_barcode_scanner/learn", "ean": "QR-<b>X</b>", "name": "Brötchen",
        "brand": None, "target_list": "todo.picnic", "add_to_list": True,
    }
    assert out["afterLearnEmpty"] and out["productsShown"] and out["searchEmpty"]
    assert out["searchKeepsFocus"]
    assert out["scanCall"] == {"type": "shys_barcode_scanner/scan", "ean": "4337256722261"}
    assert out["flash"] == "ja! Butter: Auf der Liste"
    assert out["reloadLogic"] is True
    # Offline-Katalog-Tab
    assert out["catalogNotInstalled"] is True
    assert out["catalogBoxes"] == [["food", True], ["beauty", True], ["products", True], ["petfood", True]]
    assert out["downloadCall"] == {
        "type": "shys_barcode_scanner/catalog_download", "categories": ["food", "beauty", "products"],
    }
    assert out["catalogInstalledShown"] is True
    assert out["searchCall"] == {
        "type": "shys_barcode_scanner/catalog_search", "query": "haribo", "category": None, "limit": 30,
    }
    assert out["searchResultEscaped"] is True
    assert out["searchKeepsFocusCatalog"] is True
    assert out["addCall"] == {
        "type": "shys_barcode_scanner/catalog_add", "ean": "4001686009727", "add_to_list": True,
    }
    assert out["addFlash"] == "Haribo Stars: Auf der Liste"
    assert out["offlinePill"] is True and out["offlineChecked"] is True
    assert out["savedOffline"] is True
