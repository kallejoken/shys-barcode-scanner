"""Lokaler deutscher Offline-Produktkatalog (SQLite) aus den ODbL-Datenbanken des Open-Food-Facts-Projekts.

Der Katalog liegt bewusst in einer eigenen SQLite-Datei (<config>/shys_barcode_scanner_catalog.db)
und NICHT im JSON-Store der persönlichen Produktliste: so bleiben Panel, Store und HA-Backups klein.

Alle SQLite-Zugriffe laufen im Executor (nie im Event-Loop). Ein Download baut die neue Datenbank
in einer temporären Datei und tauscht sie per os.replace atomar aus, laufende Scans sehen also nie
eine halbe Datenbank.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import io
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

from .const import (
    CATALOG_ATTRIBUTION,
    CATALOG_CATEGORIES,
    CATALOG_IMPORT_PRIORITY,
    CATALOG_LICENSE,
    CATALOG_MANIFEST_FILE,
    CATALOG_MAX_FILE_BYTES,
    CATALOG_SCHEMA_VERSION,
    DEFAULT_CATALOG_BASE_URL,
)

_LOGGER = logging.getLogger(__name__)

BATCH_SIZE = 5000
SQLITE_TIMEOUT = 10
MAX_NAME = 200
MAX_BRAND = 80


class CatalogError(Exception):
    """Fehler beim Laden/Prüfen des Offline-Katalogs (Text ist für das Panel gedacht)."""


def utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_categories(categories: Iterable[Any] | None) -> list[str]:
    """Nur bekannte Kategorien, feste Reihenfolge (food, beauty, products, petfood), ohne Duplikate."""
    wanted = {str(c).strip() for c in (categories or [])}
    unknown = wanted - set(CATALOG_CATEGORIES)
    if unknown:
        raise ValueError(f"Unbekannte Kategorie(n): {', '.join(sorted(unknown))}")
    return [key for key in CATALOG_CATEGORIES if key in wanted]


# ---------------------------------------------------------------------------
# Synchrone Bausteine (laufen im Executor, direkt testbar)
# ---------------------------------------------------------------------------


def iter_tsv_rows(path: str) -> Iterator[tuple[str, str, str]]:
    """Liest eine catalog-de-*.tsv.gz (Kopfzeile optional) als (ean, brand, name)."""
    with gzip.open(path, "rb") as raw, io.TextIOWrapper(raw, encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh):
            parts = line.rstrip("\r\n").split("\t")
            if line_no == 0 and parts[0] == "ean":
                continue
            if len(parts) < 3:
                continue
            ean = parts[0].strip()
            brand = " ".join(parts[1].split())[:MAX_BRAND]
            name = " ".join("\t".join(parts[2:]).split())[:MAX_NAME]
            if not ean.isdigit() or not 8 <= len(ean) <= 14 or not name:
                continue
            yield ean, brand, name


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_sidecars(path: str) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        with contextlib.suppress(FileNotFoundError):
            os.remove(path + suffix)


def build_catalog_db(
    db_path: str,
    files: list[tuple[str, str]],
    meta: dict[str, Any],
) -> dict[str, int]:
    """Baut die Katalog-DB aus (kategorie, tsv.gz-Pfad) und tauscht sie atomar gegen db_path.

    Bei doppelten EANs gewinnt die Kategorie, die zuerst in files steht.
    Gibt die tatsächlich importierten Produktzahlen pro Kategorie zurück.
    """
    tmp_path = db_path + ".tmp"
    with contextlib.suppress(FileNotFoundError):
        os.remove(tmp_path)
    _remove_sidecars(tmp_path)
    conn = sqlite3.connect(tmp_path, timeout=SQLITE_TIMEOUT)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute(
            "CREATE TABLE products (ean TEXT PRIMARY KEY, brand TEXT NOT NULL, "
            "name TEXT NOT NULL, category TEXT NOT NULL) WITHOUT ROWID"
        )
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        counts: dict[str, int] = {}
        with conn:  # eine einzige Transaktion
            for category, path in files:
                before = conn.total_changes
                batch: list[tuple[str, str, str, str]] = []
                for ean, brand, name in iter_tsv_rows(path):
                    batch.append((ean, brand, name, category))
                    if len(batch) >= BATCH_SIZE:
                        conn.executemany("INSERT OR IGNORE INTO products VALUES (?,?,?,?)", batch)
                        batch.clear()
                if batch:
                    conn.executemany("INSERT OR IGNORE INTO products VALUES (?,?,?,?)", batch)
                counts[category] = conn.total_changes - before
            full_meta = {
                **{k: str(v) for k, v in meta.items()},
                "installed_categories": json.dumps(
                    [c for c in CATALOG_CATEGORIES if c in {cat for cat, _ in files}]
                ),
                "counts_json": json.dumps(counts),
                "schema_version": str(CATALOG_SCHEMA_VERSION),
            }
            conn.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", list(full_meta.items()))
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except BaseException:
        conn.close()
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp_path)
        _remove_sidecars(tmp_path)
        raise
    conn.close()
    _remove_sidecars(tmp_path)
    os.replace(tmp_path, db_path)
    _remove_sidecars(db_path)
    return counts


def _row_to_dict(row: tuple[str, str, str, str]) -> dict[str, str]:
    return {"ean": row[0], "brand": row[1], "name": row[2], "category": row[3]}


def _like_variants(term: str) -> list[str]:
    """LIKE ist in SQLite nur für ASCII case-insensitiv -> Varianten für Umlaute ergänzen."""
    variants = [term]
    if not term.isascii():
        for variant in (term.lower(), term.upper(), term.capitalize()):
            if variant not in variants:
                variants.append(variant)
    return variants


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_search_sql(query: str, category: str | None, limit: int) -> tuple[str, list[Any]]:
    """SQL für die Panel-Suche: EAN-Präfix (nur Ziffern) oder alle Wörter in Marke+Name."""
    where: list[str] = []
    params: list[Any] = []
    query = " ".join((query or "").split())
    if query.isdigit():
        where.append("ean >= ? AND ean < ?")
        params += [query, query + ":"]  # ':' folgt in ASCII direkt auf '9' -> Präfix-Bereich
    else:
        for term in query.split(" ")[:6]:
            ors = []
            for variant in _like_variants(term):
                ors.append("(brand || ' ' || name) LIKE ? ESCAPE '\\'")
                params.append(f"%{_escape_like(variant)}%")
            where.append("(" + " OR ".join(ors) + ")")
    if category:
        where.append("category = ?")
        params.append(category)
    sql = "SELECT ean, brand, name, category FROM products"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY length(name), name LIMIT ?"
    params.append(limit)
    return sql, params


class CatalogDatabase:
    """Thread-sicherer, wiederverwendeter Lesezugriff auf die SQLite-Datei (synchron)."""

    def __init__(self, db_path: str) -> None:
        self.path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.close()
                self._conn = None

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def _connection(self) -> sqlite3.Connection | None:
        if self._conn is None:
            if not self.exists():
                return None
            conn = sqlite3.connect(self.path, timeout=SQLITE_TIMEOUT, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA query_only=ON")
            self._conn = conn
        return self._conn

    def _run(self, func: Callable[[sqlite3.Connection], Any], default: Any = None) -> Any:
        with self._lock:
            conn = self._connection()
            if conn is None:
                return default
            return func(conn)

    def lookup(self, ean: str) -> dict[str, str] | None:
        code = (ean or "").strip()
        if not code.isdigit():
            return None

        def run(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT ean, brand, name, category FROM products WHERE ean = ?", (code,)
            ).fetchone()
            if row is None and len(code) == 12:
                # UPC-A wird in den Dumps oft als EAN-13 mit führender 0 geführt
                row = conn.execute(
                    "SELECT ean, brand, name, category FROM products WHERE ean = ?", ("0" + code,)
                ).fetchone()
            return _row_to_dict(row) if row else None

        return self._run(run)

    def search(self, query: str, category: str | None = None, limit: int = 30) -> list[dict[str, str]]:
        query = (query or "").strip()
        if not query:
            return []
        sql, params = build_search_sql(query, category, max(1, min(int(limit), 200)))
        return self._run(lambda conn: [_row_to_dict(r) for r in conn.execute(sql, params)], [])

    def meta(self) -> dict[str, str]:
        return self._run(lambda conn: dict(conn.execute("SELECT key, value FROM meta")), {})

    def replace(self, files: list[tuple[str, str]], meta: dict[str, Any]) -> dict[str, int]:
        with self._lock:
            self.close()
            return build_catalog_db(self.path, files, meta)

    def delete(self) -> bool:
        with self._lock:
            self.close()
            existed = self.exists()
            for path in (self.path, self.path + ".tmp"):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(path)
                _remove_sidecars(path)
            return existed

    def status(self) -> dict[str, Any]:
        with self._lock:
            if not self.exists():
                return {"installed": False}
            try:
                meta = self.meta()
            except sqlite3.Error as err:
                return {"installed": False, "error": f"Katalog-Datei beschädigt: {err}"}
            size = sum(
                os.path.getsize(p)
                for p in (self.path, self.path + "-wal")
                if os.path.isfile(p)
            )
            return {"installed": True, "meta": meta, "size_bytes": size}


# ---------------------------------------------------------------------------
# Async-Fassade für Home Assistant
# ---------------------------------------------------------------------------


class OfflineCatalog:
    """Verwaltet Download, Status, Lookup und Suche des lokalen Offline-Katalogs."""

    def __init__(
        self,
        hass: Any,
        db_path: str,
        session: Any,
        *,
        base_url: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.hass = hass
        self.db = CatalogDatabase(db_path)
        self._session = session
        self.base_url = (base_url or DEFAULT_CATALOG_BASE_URL).rstrip("/")
        self.user_agent = user_agent or "SHYSBarcodeScanner"
        self._download_lock = asyncio.Lock()
        self._installed: bool | None = None
        self.progress: dict[str, Any] | None = None

    @property
    def path(self) -> str:
        return self.db.path

    def set_base_url(self, base_url: str | None) -> None:
        self.base_url = (base_url or DEFAULT_CATALOG_BASE_URL).rstrip("/")

    @property
    def busy(self) -> bool:
        return self._download_lock.locked()

    async def _run(self, func: Callable[..., Any], *args: Any) -> Any:
        return await self.hass.async_add_executor_job(func, *args)

    # ------------------------------------------------------------- Lesen
    async def async_is_installed(self) -> bool:
        if self._installed is None:
            self._installed = await self._run(self.db.exists)
        return self._installed

    async def async_lookup(self, ean: str) -> dict[str, str] | None:
        """Primary-Key-Lookup. Fehler werden geloggt, ein Scan darf daran nie scheitern."""
        if not await self.async_is_installed():
            return None
        try:
            return await self._run(self.db.lookup, ean)
        except sqlite3.Error as err:
            _LOGGER.warning("Offline-Katalog: Lookup für %s fehlgeschlagen: %s", ean, err)
            return None

    async def async_search(
        self, query: str, category: str | None = None, limit: int = 30
    ) -> list[dict[str, str]]:
        if category and category not in CATALOG_CATEGORIES:
            raise ValueError(f"Unbekannte Kategorie: {category}")
        if not await self.async_is_installed():
            return []
        return await self._run(self.db.search, query, category, limit)

    async def async_status(self) -> dict[str, Any]:
        raw = await self._run(self.db.status)
        self._installed = bool(raw.get("installed"))
        meta = raw.get("meta") or {}
        try:
            counts = json.loads(meta.get("counts_json") or "{}")
        except ValueError:
            counts = {}
        try:
            installed = json.loads(meta.get("installed_categories") or "[]")
        except ValueError:
            installed = []
        size = int(raw.get("size_bytes") or 0)
        return {
            "installed": self._installed,
            "error": raw.get("error"),
            "busy": self.busy,
            "progress": self.progress,
            "path": self.path,
            "generated_at": meta.get("generated_at"),
            "downloaded_at": meta.get("downloaded_at"),
            "source_url": meta.get("source_url"),
            "installed_categories": installed,
            "counts": counts,
            "total_count": sum(int(v) for v in counts.values()),
            "size_bytes": size,
            "size_mb": round(size / 1_048_576, 1),
            "base_url": self.base_url,
            "license": CATALOG_LICENSE,
            "attribution": CATALOG_ATTRIBUTION,
            "categories": [
                {"key": key, **info, "installed": key in installed, "count": int(counts.get(key, 0))}
                for key, info in CATALOG_CATEGORIES.items()
            ],
        }

    # ------------------------------------------------------------ Online
    def _timeout(self, total: float) -> Any:
        import aiohttp

        return aiohttp.ClientTimeout(total=total, sock_connect=20, sock_read=60)

    async def async_fetch_manifest(self) -> dict[str, Any]:
        url = f"{self.base_url}/{CATALOG_MANIFEST_FILE}"
        try:
            async with self._session.get(
                url, headers={"User-Agent": self.user_agent}, timeout=self._timeout(30)
            ) as resp:
                if resp.status != 200:
                    raise CatalogError(f"Manifest nicht abrufbar (HTTP {resp.status}): {url}")
                text = await resp.text()
        except CatalogError:
            raise
        except (TimeoutError, OSError) as err:
            raise CatalogError(f"Manifest nicht erreichbar: {err or type(err).__name__}") from err
        except Exception as err:  # aiohttp.ClientError & Co.
            raise CatalogError(f"Manifest nicht erreichbar: {err or type(err).__name__}") from err
        try:
            manifest = json.loads(text)
        except ValueError as err:
            raise CatalogError("Manifest ist kein gültiges JSON") from err
        if not isinstance(manifest, dict) or manifest.get("schema_version") != CATALOG_SCHEMA_VERSION:
            raise CatalogError("Manifest hat ein unbekanntes Format (schema_version)")
        if not isinstance(manifest.get("categories"), dict):
            raise CatalogError("Manifest enthält keine Kategorien")
        return manifest

    async def async_check_manifest(self) -> dict[str, Any]:
        """Online-Stand prüfen: verfügbare Kategorien, Zahlen, Größen und ob ein Update vorliegt."""
        manifest = await self.async_fetch_manifest()
        status = await self.async_status()
        categories = []
        for key, info in CATALOG_CATEGORIES.items():
            remote = manifest["categories"].get(key)
            if not isinstance(remote, dict):
                continue
            categories.append(
                {
                    "key": key,
                    "label_de": remote.get("label_de") or info["label_de"],
                    "label_en": remote.get("label_en") or info["label_en"],
                    "source": info["source"],
                    "count": int(remote.get("count") or 0),
                    "size_bytes": int(remote.get("size_bytes") or 0),
                    "size_mb": round(int(remote.get("size_bytes") or 0) / 1_048_576, 1),
                    "installed": key in status["installed_categories"],
                }
            )
        generated = manifest.get("generated_at")
        update_available = bool(
            status["installed"] and generated and generated != status.get("generated_at")
        )
        return {
            "available": True,
            "base_url": self.base_url,
            "generated_at": generated,
            "license": manifest.get("license") or CATALOG_LICENSE,
            "attribution": manifest.get("attribution") or CATALOG_ATTRIBUTION,
            "categories": categories,
            "installed": status["installed"],
            "installed_generated_at": status.get("generated_at"),
            "update_available": update_available,
        }

    async def _async_download_file(self, url: str, dest: str, expected_sha: str, max_bytes: int) -> int:
        """Datei streamen, dabei SHA256 rechnen; bei Abweichung CatalogError."""
        digest = hashlib.sha256()
        size = 0
        fh = await self._run(open, dest, "wb")
        try:
            async with self._session.get(
                url, headers={"User-Agent": self.user_agent}, timeout=self._timeout(900)
            ) as resp:
                if resp.status != 200:
                    raise CatalogError(f"Download fehlgeschlagen (HTTP {resp.status}): {url}")
                async for chunk in resp.content.iter_chunked(1 << 16):
                    size += len(chunk)
                    if size > max_bytes:
                        raise CatalogError(f"Datei ist größer als erlaubt: {url}")
                    digest.update(chunk)
                    await self._run(fh.write, chunk)
        except CatalogError:
            raise
        except Exception as err:  # Netzwerk/Timeout
            raise CatalogError(f"Download fehlgeschlagen: {err or type(err).__name__}") from err
        finally:
            await self._run(fh.close)
        if digest.hexdigest().lower() != (expected_sha or "").strip().lower():
            raise CatalogError(f"Prüfsumme (SHA256) stimmt nicht: {url.rsplit('/', 1)[-1]}")
        return size

    async def async_download_categories(self, categories: list[str]) -> dict[str, Any]:
        """Gewählte Kategorien laden, SHA256 prüfen, SQLite neu bauen und atomar tauschen."""
        wanted = normalize_categories(categories)
        if not wanted:
            raise ValueError("Bitte mindestens eine Kategorie wählen.")
        if self._download_lock.locked():
            raise CatalogError("Es läuft bereits ein Katalog-Download.")
        async with self._download_lock:
            downloaded: list[str] = []
            try:
                self.progress = {"step": "manifest"}
                manifest = await self.async_fetch_manifest()
                files: list[tuple[str, str]] = []
                for key in wanted:
                    info = manifest["categories"].get(key)
                    if not isinstance(info, dict) or not info.get("file") or not info.get("sha256"):
                        raise CatalogError(f"Kategorie {key} ist im Manifest nicht verfügbar.")
                    file_name = os.path.basename(str(info["file"]))
                    dest = f"{self.path}.{key}.download"
                    downloaded.append(dest)
                    self.progress = {"step": "download", "category": key}
                    await self._async_download_file(
                        f"{self.base_url}/{file_name}", dest, str(info["sha256"]), CATALOG_MAX_FILE_BYTES
                    )
                    files.append((key, dest))
                self.progress = {"step": "import"}
                files.sort(key=lambda item: CATALOG_IMPORT_PRIORITY.index(item[0]))
                meta = {
                    "generated_at": manifest.get("generated_at") or "",
                    "source_url": self.base_url,
                    "downloaded_at": utcnow_iso(),
                    "license": manifest.get("license") or CATALOG_LICENSE,
                }
                await self._run(self.db.replace, files, meta)
            finally:
                self.progress = None
                for path in downloaded:
                    with contextlib.suppress(FileNotFoundError):
                        await self._run(os.remove, path)
            self._installed = True
        return await self.async_status()

    async def async_delete(self) -> bool:
        if self._download_lock.locked():
            raise CatalogError("Es läuft gerade ein Katalog-Download.")
        removed = await self._run(self.db.delete)
        self._installed = False
        return removed

    async def async_close(self) -> None:
        await self._run(self.db.close)
