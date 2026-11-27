"""Baut den deutschen Offline-Produktkatalog aus den vier ODbL-Datenbanken des Open-Food-Facts-Projekts.

Quellen (offizielle CSV-Exporte, tab-getrennt, gzip):
  food      Open Food Facts      (Lebensmittel & Getränke)
  beauty    Open Beauty Facts    (Drogerie & Kosmetik)
  products  Open Products Facts  (Haushalt & Alltag)
  petfood   Open Pet Food Facts  (Tierfutter)

Die Dumps werden gestreamt (Download -> gzip -> Zeilen), es landet nichts Großes auf der Platte
und der RAM-Bedarf bleibt klein (nur die gefilterten deutschen Treffer werden gehalten).

Ausgabe in --out:
  catalog-de-<kategorie>.tsv.gz   UTF-8 TSV mit Kopfzeile "ean<TAB>brand<TAB>name", sortiert nach EAN
  catalog-de-manifest.json        Stand, Lizenz, Zahlen, Größen und SHA256 pro Kategorie

Aufruf:
  python3 scripts/build_offline_catalog.py --out dist/catalog
  python3 scripts/build_offline_catalog.py --out /tmp/cat --categories beauty petfood
  python3 scripts/build_offline_catalog.py --out /tmp/cat --source food=/pfad/zu/dump.csv.gz

Nur Python-Standardbibliothek, läuft daher ohne Installation in jeder GitHub Action.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import sys
import time
import urllib.request
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1
USER_AGENT = (
    "SHYS-Barcode-Scanner-CatalogBuilder/1.4 "
    "(+https://github.com/kallejoken/shys-barcode-scanner)"
)
LICENSE = "ODbL (Open Database License)"
ATTRIBUTION = (
    "Datenquelle: Open Food Facts, Open Beauty Facts, Open Products Facts, "
    "Open Pet Food Facts (ODbL)"
)

CATEGORIES: dict[str, dict[str, str]] = {
    "food": {
        "label_de": "Lebensmittel & Getränke",
        "label_en": "Food & Beverages",
        "source": "Open Food Facts",
        "url": "https://static.openfoodfacts.org/data/en.openfoodfacts.org.products.csv.gz",
    },
    "beauty": {
        "label_de": "Drogerie & Kosmetik",
        "label_en": "Beauty & Personal Care",
        "source": "Open Beauty Facts",
        "url": "https://static.openbeautyfacts.org/data/en.openbeautyfacts.org.products.csv.gz",
    },
    "products": {
        "label_de": "Haushalt & Alltag",
        "label_en": "Household & Everyday",
        "source": "Open Products Facts",
        "url": "https://static.openproductsfacts.org/data/en.openproductsfacts.org.products.csv.gz",
    },
    "petfood": {
        "label_de": "Tierfutter",
        "label_en": "Pet Food",
        "source": "Open Pet Food Facts",
        "url": "https://static.openpetfoodfacts.org/data/en.openpetfoodfacts.org.products.csv.gz",
    },
}

GERMANY_TAG = "en:germany"
GERMANY_WORDS = {"germany", "deutschland", "allemagne", "alemania", "germania", "de", "en:de"}
MAX_BRAND = 80
MAX_NAME = 200

_WS_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"^(https?://|www\.)", re.IGNORECASE)
_HAS_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_TAG_PREFIX_RE = re.compile(r"^[a-z]{2}:")


# ---------------------------------------------------------------------------
# Filter- und Bereinigungslogik (auch von den Unit-Tests genutzt)
# ---------------------------------------------------------------------------


def ean_checksum_ok(code: str) -> bool:
    """Prüft die GS1-Prüfziffer einer EAN-8/EAN-13."""
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    # Von rechts: Gewichtung 3,1,3,1,...
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def normalize_ean(raw: str) -> str | None:
    """Gibt eine gültige EAN-8/EAN-13 zurück oder None.

    Abgelehnt werden: Nicht-Ziffern, falsche Länge, falsche Prüfziffer, Dummy-Codes
    (nur gleiche Ziffern vor der Prüfziffer, fast nur Nullen) und Codes mit eingeschränkter Verbreitung
    (Präfix 2 = händlerinterne Codes, z. B. Waagen-Etiketten mit eingebettetem Preis).
    """
    code = (raw or "").strip()
    if not code.isdigit() or not code.isascii() or len(code) not in (8, 13):
        return None
    if len(set(code[:-1])) == 1:  # 1111111111116, 00000000 ...
        return None
    if code.startswith("2"):
        return None
    if len(code) == 13 and code.startswith("0000000"):
        return None
    if len(code) == 8 and code.startswith("0000"):
        return None
    if not ean_checksum_ok(code):
        return None
    return code


def clean_text(value: str | None, max_len: int) -> str:
    text = _WS_RE.sub(" ", (value or "").replace("\\t", " ").replace("\\n", " ")).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip(" ,;-")
    return text


def clean_name(value: str | None) -> str:
    name = clean_text(value, MAX_NAME)
    if len(name) < 2 or _URL_RE.match(name) or not _HAS_LETTER_RE.search(name):
        return ""
    return name


def clean_brand(*candidates: str | None) -> str:
    """Erste Marke aus 'brands', ersatzweise 'brands_en' bzw. 'brands_tags' (ohne 'xx:'-Präfix)."""
    for raw in candidates:
        if not raw:
            continue
        first = raw.split(",")[0]
        first = _TAG_PREFIX_RE.sub("", first.strip())
        brand = clean_text(first, MAX_BRAND)
        if brand and _HAS_LETTER_RE.search(brand) and not _URL_RE.match(brand):
            return brand
    return ""


def is_german(countries_tags: str | None, *countries_text: str | None) -> bool:
    tags = [t.strip().lower() for t in (countries_tags or "").split(",") if t.strip()]
    if tags:
        return GERMANY_TAG in tags
    for text in countries_text:
        for token in (text or "").split(","):
            if token.strip().lower() in GERMANY_WORDS:
                return True
    return False


def _col(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return row[idx]


def iter_filtered(lines: Iterable[str], stats: dict[str, int] | None = None) -> Iterator[tuple[str, str, str, int]]:
    """Liest einen OFF-CSV-Export (Kopfzeile + tab-getrennte Zeilen) und liefert (ean, brand, name, score).

    Spalten werden über die Kopfzeile gefunden, damit leicht abweichende Exporte
    (z. B. 'brands' fehlt, nur 'brands_en'/'brands_tags') sauber funktionieren.
    """
    stats = stats if stats is not None else {}
    for key in ("rows", "german", "valid_ean", "kept"):
        stats.setdefault(key, 0)
    it = iter(lines)
    try:
        header = next(it).rstrip("\r\n").split("\t")
    except StopIteration:
        return
    index = {name.strip().lstrip("\ufeff"): i for i, name in enumerate(header)}
    if "code" not in index:
        raise ValueError("CSV-Export ohne Spalte 'code' – unbekanntes Format")
    i_code = index["code"]
    i_name_de = index.get("product_name_de")
    i_name = index.get("product_name")
    i_brands = index.get("brands")
    i_brands_en = index.get("brands_en")
    i_brands_tags = index.get("brands_tags")
    i_ctags = index.get("countries_tags")
    i_countries = index.get("countries")
    i_countries_en = index.get("countries_en")

    for line in it:
        stats["rows"] += 1
        row = line.rstrip("\r\n").split("\t")
        if not is_german(_col(row, i_ctags), _col(row, i_countries), _col(row, i_countries_en)):
            continue
        stats["german"] += 1
        ean = normalize_ean(_col(row, i_code))
        if ean is None:
            continue
        stats["valid_ean"] += 1
        name_de = clean_name(_col(row, i_name_de))
        name = name_de or clean_name(_col(row, i_name))
        if not name:
            continue
        brand = clean_brand(_col(row, i_brands), _col(row, i_brands_en), _col(row, i_brands_tags))
        score = (4 if name_de else 0) + (2 if brand else 0) + min(len(name), 100) // 25
        stats["kept"] += 1
        yield ean, brand, name, score


def dedupe(entries: Iterable[tuple[str, str, str, int]]) -> dict[str, tuple[str, str]]:
    """Pro EAN den vollständigsten Eintrag behalten (deutscher Name > Marke > längerer Name)."""
    best: dict[str, tuple[int, str, str]] = {}
    for ean, brand, name, score in entries:
        current = best.get(ean)
        if current is None or score > current[0]:
            best[ean] = (score, brand, name)
    return {ean: (brand, name) for ean, (_s, brand, name) in best.items()}


def write_tsv_gz(path: Path, products: dict[str, tuple[str, str]]) -> None:
    """Schreibt deterministisch (mtime=0, sortiert), damit gleiche Daten denselben SHA256 haben."""
    with (
        open(path, "wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz,
        io.TextIOWrapper(gz, encoding="utf-8", newline="\n") as out,
    ):
        out.write("ean\tbrand\tname\n")
        for ean in sorted(products):
            brand, name = products[ean]
            out.write(f"{ean}\t{brand}\t{name}\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Streaming-Quellen
# ---------------------------------------------------------------------------


class _Progress(io.RawIOBase):
    """Zählt gelesene (komprimierte) Bytes für die Fortschrittsanzeige."""

    def __init__(self, raw, label: str, total: int | None) -> None:
        self._raw = raw
        self.label = label
        self.total = total
        self.read_bytes = 0
        self._last = 0.0

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        data = self._raw.read(len(b))
        n = len(data)
        b[:n] = data
        self.read_bytes += n
        now = time.monotonic()
        if now - self._last > 30:
            self._last = now
            pct = f"{self.read_bytes / self.total * 100:5.1f} %" if self.total else ""
            print(f"  [{self.label}] {self.read_bytes / 1e6:8.1f} MB {pct}", file=sys.stderr, flush=True)
        return n


def open_source_lines(source: str, label: str, user_agent: str):
    """Öffnet URL oder lokale Datei (.csv oder .csv.gz) als Zeilen-Stream. Gibt (lines, info, closer) zurück."""
    info: dict[str, str | int | None] = {"source": source}
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"User-Agent": user_agent})
        response = urllib.request.urlopen(request, timeout=120)
        info["last_modified"] = response.headers.get("Last-Modified")
        length = response.headers.get("Content-Length")
        info["download_bytes"] = int(length) if length and length.isdigit() else None
        raw = io.BufferedReader(_Progress(response, label, info["download_bytes"]), buffer_size=1 << 20)
        closer = response
        gzipped = True
        if not source.endswith(".gz"):
            gzipped = response.headers.get("Content-Type", "").endswith("gzip")
    else:
        path = Path(source)
        raw = open(path, "rb")  # noqa: SIM115
        closer = raw
        gzipped = path.suffix == ".gz"
        info["download_bytes"] = path.stat().st_size
    stream = gzip.GzipFile(fileobj=raw) if gzipped else raw
    text = io.TextIOWrapper(stream, encoding="utf-8", errors="replace", newline="\n")
    return text, info, closer


def build_category(key: str, source: str, out_dir: Path, user_agent: str, retries: int = 3) -> dict:
    meta = CATEGORIES[key]
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        stats: dict[str, int] = {}
        started = time.monotonic()
        try:
            lines, info, closer = open_source_lines(source, key, user_agent)
            try:
                products = dedupe(iter_filtered(lines, stats))
            finally:
                closer.close()
            break
        except Exception as err:  # noqa: BLE001 - Netzwerkabbrüche beim Streaming wiederholen
            last_error = err
            print(f"  [{key}] Versuch {attempt}/{retries} fehlgeschlagen: {err!r}", file=sys.stderr, flush=True)
            time.sleep(10 * attempt)
    else:
        raise RuntimeError(f"Kategorie {key} konnte nicht gebaut werden: {last_error!r}")

    file_name = f"catalog-de-{key}.tsv.gz"
    path = out_dir / file_name
    write_tsv_gz(path, products)
    entry = {
        "label_de": meta["label_de"],
        "label_en": meta["label_en"],
        "source": meta["source"],
        "source_url": source if source.startswith("http") else meta["url"],
        "source_last_modified": info.get("last_modified"),
        "file": file_name,
        "count": len(products),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    print(
        f"  [{key}] fertig in {time.monotonic() - started:.0f} s: {stats.get('rows', 0)} Zeilen, "
        f"{stats.get('german', 0)} DE, {stats.get('valid_ean', 0)} gültige EAN, {len(products)} eindeutig "
        f"-> {file_name} ({entry['size_bytes'] / 1e6:.2f} MB)",
        file=sys.stderr,
        flush=True,
    )
    entry["stats"] = stats
    return entry


def build(out_dir: Path, categories: list[str], sources: dict[str, str], user_agent: str = USER_AGENT) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "license": LICENSE,
        "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
        "attribution": ATTRIBUTION,
        "filter": "countries_tags enthält en:germany; gültige EAN-8/EAN-13; Produktname vorhanden",
        "categories": {},
    }
    for key in categories:
        print(f"[{key}] {sources.get(key, CATEGORIES[key]['url'])}", file=sys.stderr, flush=True)
        manifest["categories"][key] = build_category(key, sources.get(key, CATEGORIES[key]["url"]), out_dir, user_agent)
    manifest["total_count"] = sum(c["count"] for c in manifest["categories"].values())
    manifest_path = out_dir / "catalog-de-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="dist/catalog", type=Path, help="Ausgabeordner")
    parser.add_argument("--categories", nargs="+", choices=list(CATEGORIES), default=list(CATEGORIES))
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="KAT=URL_ODER_DATEI",
        help="Quelle einer Kategorie überschreiben (z. B. für Tests mit lokalem Dump)",
    )
    parser.add_argument("--user-agent", default=USER_AGENT)
    args = parser.parse_args(argv)
    sources: dict[str, str] = {}
    for item in args.source:
        key, _, value = item.partition("=")
        if key not in CATEGORIES or not value:
            parser.error(f"Ungültige --source-Angabe: {item}")
        sources[key] = value
    manifest = build(args.out, args.categories, sources, args.user_agent)
    print(json.dumps({k: v["count"] for k, v in manifest["categories"].items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
