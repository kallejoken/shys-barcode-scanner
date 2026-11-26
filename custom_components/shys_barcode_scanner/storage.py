"""Lokale, selbstlernende Produktdatenbank (HA-Store, landet im HA-Backup)."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Protocol

from .const import MAX_CODE_LENGTH, SOURCE_IMPORT, SOURCE_MANUAL


class StoreLike(Protocol):
    """Minimales Interface von homeassistant.helpers.storage.Store."""

    async def async_load(self) -> dict[str, Any] | None: ...

    async def async_save(self, data: dict[str, Any]) -> None: ...


def utcnow_iso() -> str:
    """Aktuelle Zeit als ISO-String (UTC)."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def clean_code(raw: Any) -> str:
    """Code säubern: Steuerzeichen raus, Leerraum außen weg."""
    if raw is None:
        return ""
    text = str(raw)
    text = "".join(ch for ch in text if ch.isprintable())
    return text.strip()[:MAX_CODE_LENGTH]


def _clean_text(value: Any, max_len: int = 255) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:max_len]


class BarcodeStorage:
    """Produkte (gelernt) und offene, unbekannte Scans."""

    def __init__(self, store: StoreLike) -> None:
        self._store = store
        self.products: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ IO
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.products = {
            str(k): v for k, v in (data.get("products") or {}).items() if isinstance(v, dict)
        }
        self.pending = {
            str(k): v for k, v in (data.get("pending") or {}).items() if isinstance(v, dict)
        }

    async def async_save(self) -> None:
        await self._store.async_save(self._as_dict())

    def _as_dict(self) -> dict[str, Any]:
        return {"products": self.products, "pending": self.pending}

    # ------------------------------------------------------------ Produkte
    def get_product(self, ean: str) -> dict[str, Any] | None:
        return self.products.get(clean_code(ean))

    async def upsert_product(
        self,
        ean: str,
        name: str,
        brand: str | None = None,
        source: str = SOURCE_MANUAL,
        target_list: str | None = None,
        *,
        save: bool = True,
    ) -> dict[str, Any]:
        """Produkt anlegen oder aktualisieren. Entfernt es aus den offenen Scans."""
        code = clean_code(ean)
        name = _clean_text(name)
        if not code:
            raise ValueError("Code darf nicht leer sein")
        if not name:
            raise ValueError("Name darf nicht leer sein")
        now = utcnow_iso()
        existing = self.products.get(code)
        pending = self.pending.pop(code, None)
        if existing is None:
            product = {
                "ean": code,
                "name": name,
                "brand": _clean_text(brand),
                "source": source,
                "target_list": target_list or None,
                "scan_count": int((pending or {}).get("scan_count", 0)),
                "created_at": now,
                "updated_at": now,
                "last_scanned": (pending or {}).get("last_seen"),
            }
        else:
            product = {
                **existing,
                "name": name,
                "brand": _clean_text(brand) if brand is not None else existing.get("brand", ""),
                "source": source or existing.get("source", SOURCE_MANUAL),
                "target_list": target_list or None,
                "updated_at": now,
            }
        self.products[code] = product
        if save:
            await self.async_save()
        return product

    async def touch_product(self, ean: str, *, save: bool = True) -> None:
        """Scan-Zähler und Zeitstempel hochsetzen."""
        product = self.products.get(clean_code(ean))
        if product is None:
            return
        product["scan_count"] = int(product.get("scan_count", 0)) + 1
        product["last_scanned"] = utcnow_iso()
        if save:
            await self.async_save()

    async def delete_product(self, ean: str) -> bool:
        removed = self.products.pop(clean_code(ean), None) is not None
        if removed:
            await self.async_save()
        return removed

    def list_products(self) -> list[dict[str, Any]]:
        return sorted(
            (deepcopy(p) for p in self.products.values()),
            key=lambda p: (p.get("name") or "").casefold(),
        )

    # ------------------------------------------------------ offene Scans
    async def add_pending(self, ean: str, device: str | None = None) -> tuple[dict[str, Any], bool]:
        """Unbekannten Scan merken. Rückgabe: (Eintrag, neu_angelegt)."""
        code = clean_code(ean)
        if not code:
            raise ValueError("Code darf nicht leer sein")
        now = utcnow_iso()
        entry = self.pending.get(code)
        is_new = entry is None
        if is_new:
            entry = {
                "ean": code,
                "first_seen": now,
                "last_seen": now,
                "scan_count": 1,
                "device": device or None,
            }
            self.pending[code] = entry
        else:
            entry["last_seen"] = now
            entry["scan_count"] = int(entry.get("scan_count", 0)) + 1
            if device:
                entry["device"] = device
        await self.async_save()
        return deepcopy(entry), is_new

    async def remove_pending(self, ean: str) -> bool:
        removed = self.pending.pop(clean_code(ean), None) is not None
        if removed:
            await self.async_save()
        return removed

    def list_pending(self) -> list[dict[str, Any]]:
        return sorted(
            (deepcopy(p) for p in self.pending.values()),
            key=lambda p: p.get("last_seen") or "",
            reverse=True,
        )

    # --------------------------------------------------- Export / Import
    def export_dict(self) -> dict[str, Any]:
        return {
            "format": "shys_barcode_scanner",
            "version": 1,
            "exported_at": utcnow_iso(),
            "products": deepcopy(self.products),
            "pending": deepcopy(self.pending),
        }

    async def import_dict(self, data: dict[str, Any], merge: bool = True) -> dict[str, int]:
        """Produkte importieren. merge=False ersetzt die Datenbank komplett.

        Akzeptiert {"products": {ean: {...}}} oder {"products": [{...}, ...]}.
        """
        if not isinstance(data, dict):
            raise ValueError("Import muss ein JSON-Objekt sein")  # noqa: TRY004 - WS-Fehler als ValueError
        raw = data.get("products")
        if raw is None:
            raise ValueError("Import enthält keine 'products'")
        if isinstance(raw, dict):
            items = []
            for key, value in raw.items():
                if isinstance(value, dict):
                    items.append({"ean": key, **value} if "ean" not in value else value)
        elif isinstance(raw, list):
            items = [v for v in raw if isinstance(v, dict)]
        else:
            raise ValueError("'products' muss Objekt oder Liste sein")  # noqa: TRY004

        now = utcnow_iso()
        new_products: dict[str, dict[str, Any]] = {} if not merge else dict(self.products)
        imported = skipped = 0
        for item in items:
            code = clean_code(item.get("ean"))
            name = _clean_text(item.get("name"))
            if not code or not name:
                skipped += 1
                continue
            old = new_products.get(code, {})
            new_products[code] = {
                "ean": code,
                "name": name,
                "brand": _clean_text(item.get("brand")),
                "source": str(item.get("source") or SOURCE_IMPORT),
                "target_list": item.get("target_list") or None,
                "scan_count": max(int(item.get("scan_count") or 0), int(old.get("scan_count") or 0)),
                "created_at": item.get("created_at") or old.get("created_at") or now,
                "updated_at": now,
                "last_scanned": item.get("last_scanned") or old.get("last_scanned"),
            }
            imported += 1

        self.products = new_products
        if not merge:
            self.pending = {}
        # Gelernte Codes sind nicht mehr offen
        for code in list(self.pending):
            if code in self.products:
                del self.pending[code]
        await self.async_save()
        return {"imported": imported, "skipped": skipped, "total": len(self.products)}
