"""EAN-Abfrage-Kaskade: Open Food Facts -> ean-suche.net -> OpengtinDB (optional).

Wirft nie Exceptions nach außen: 404, Timeouts, kaputtes JSON usw. werden
geloggt und als "nicht gefunden" bzw. Fehler in LookupResult vermerkt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .const import SOURCE_EANSUCHE, SOURCE_OPENFOODFACTS, SOURCE_OPENGTINDB

_LOGGER = logging.getLogger(__name__)

OFF_URL = (
    "https://world.openfoodfacts.org/api/v3/product/{ean}"
    "?lc=de&product_type=all"
    "&fields=product_name_de,product_name,generic_name_de,generic_name,brands"
)
# Gleicher, am Gerät verifizierter Endpunkt wie in barcode.yaml v2.3
EANSUCHE_URL = "https://ean-suche.net/api/produkt/{ean}"
OPENGTINDB_URL = "https://opengtindb.org/"

REQUEST_TIMEOUT = 6

# Ergebnis je Quelle
FOUND = "found"
NOT_FOUND = "not_found"
FAILED = "failed"


@dataclass
class LookupResult:
    """Ergebnis der Kaskade."""

    ean: str
    found: bool = False
    name: str = ""
    brand: str = ""
    source: str | None = None
    # Quelle -> found | not_found | failed (+ Grund)
    attempts: dict[str, str] = field(default_factory=dict)

    @property
    def all_failed(self) -> bool:
        """True, wenn keine Quelle eine klare Antwort geben konnte (z. B. offline)."""
        return bool(self.attempts) and all(
            v.startswith(FAILED) for v in self.attempts.values()
        )


def is_valid_gtin(code: str) -> bool:
    """EAN-8, UPC-A (12), EAN-13, GTIN-14 inklusive Prüfziffer prüfen."""
    if not isinstance(code, str) or len(code) not in (8, 12, 13, 14) or not code.isdigit():
        return False
    if not code.isascii():
        return False
    total = 0
    # Gewichte von rechts (ohne Prüfziffer): 3, 1, 3, 1, ...
    for i, ch in enumerate(reversed(code[:-1])):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return (10 - total % 10) % 10 == int(code[-1])


def _first_brand(brands: Any) -> str:
    if not brands:
        return ""
    if isinstance(brands, list):
        brands = ",".join(str(b) for b in brands)
    return str(brands).split(",")[0].strip()


def parse_openfoodfacts(data: Any) -> tuple[str, str] | None:
    """(name, marke) aus einer OFF-v3-Antwort, sonst None."""
    if not isinstance(data, dict) or data.get("status") != "success":
        return None
    product = data.get("product")
    if not isinstance(product, dict):
        return None
    name = ""
    for key in ("product_name_de", "product_name", "generic_name_de", "generic_name"):
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            name = " ".join(value.split())
            break
    if not name:
        return None
    return name, _first_brand(product.get("brands"))


def parse_eansuche(data: Any) -> str | None:
    """Produktname aus ean-suche.net ({"found": true, "product_name": ...})."""
    if not isinstance(data, dict) or not data.get("found"):
        return None
    name = data.get("product_name")
    if not isinstance(name, str) or not name.strip():
        return None
    return " ".join(name.split())


def parse_opengtindb(body: str) -> tuple[int, str, str]:
    """OpengtinDB-Textantwort ("key=value" je Zeile) -> (error, name, vendor)."""
    error_code = -1
    name = detailname = vendor = ""
    for line in body.splitlines():
        line = line.rstrip("\r")
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if key == "error":
            try:
                error_code = int(value)
            except ValueError:
                error_code = -1
        elif key == "name" and not name:
            name = value
        elif key == "detailname" and not detailname:
            detailname = value
        elif key == "vendor" and not vendor:
            vendor = value
    return error_code, (detailname or name), vendor


class EanLookup:
    """Fragt die aktivierten Quellen nacheinander ab."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        user_agent: str,
        use_openfoodfacts: bool = True,
        use_eansuche: bool = True,
        opengtindb_query_id: str = "",
        timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        self._session = session
        self._headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self.use_openfoodfacts = use_openfoodfacts
        self.use_eansuche = use_eansuche
        self.opengtindb_query_id = (opengtindb_query_id or "").strip()
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    @property
    def any_enabled(self) -> bool:
        return self.use_openfoodfacts or self.use_eansuche or bool(self.opengtindb_query_id)

    async def async_lookup(self, ean: str) -> LookupResult:
        result = LookupResult(ean=ean)
        steps = []
        if self.use_openfoodfacts:
            steps.append((SOURCE_OPENFOODFACTS, self._openfoodfacts))
        if self.use_eansuche:
            steps.append((SOURCE_EANSUCHE, self._eansuche))
        if self.opengtindb_query_id:
            steps.append((SOURCE_OPENGTINDB, self._opengtindb))

        for source, func in steps:
            try:
                hit = await func(ean)
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.warning("%s nicht erreichbar (%s): %s", source, ean, err or type(err).__name__)
                result.attempts[source] = f"{FAILED}: {type(err).__name__}"
                continue
            except Exception as err:  # noqa: BLE001 - niemals den Scan abbrechen
                _LOGGER.warning("%s: unerwarteter Fehler bei %s: %s", source, ean, err)
                result.attempts[source] = f"{FAILED}: {type(err).__name__}"
                continue
            if isinstance(hit, str):  # Fehlerbeschreibung
                result.attempts[source] = hit
                continue
            if hit is None:
                result.attempts[source] = NOT_FOUND
                continue
            name, brand = hit
            result.attempts[source] = FOUND
            result.found = True
            result.name = name
            result.brand = brand
            result.source = source
            _LOGGER.info("%s: gefunden %s -> %s / %s", source, ean, brand, name)
            return result
        return result

    # ------------------------------------------------------------ Quellen
    async def _openfoodfacts(self, ean: str) -> tuple[str, str] | str | None:
        async with self._session.get(
            OFF_URL.format(ean=ean), headers=self._headers, timeout=self._timeout
        ) as resp:
            if resp.status == 404:
                return None
            if resp.status != 200:
                _LOGGER.warning("Open Food Facts: HTTP %s für %s", resp.status, ean)
                return f"{FAILED}: HTTP {resp.status}"
            try:
                data = await resp.json(content_type=None)
            except ValueError:
                return f"{FAILED}: invalid JSON"
        return parse_openfoodfacts(data)

    async def _eansuche(self, ean: str) -> tuple[str, str] | str | None:
        async with self._session.get(
            EANSUCHE_URL.format(ean=ean), headers=self._headers, timeout=self._timeout
        ) as resp:
            if resp.status == 404:
                return None
            if resp.status == 429:
                _LOGGER.warning("ean-suche.net: Limit erreicht (ca. 60 Abfragen/Stunde)")
                return f"{FAILED}: HTTP 429"
            if resp.status != 200:
                _LOGGER.warning("ean-suche.net: HTTP %s für %s", resp.status, ean)
                return f"{FAILED}: HTTP {resp.status}"
            try:
                data = await resp.json(content_type=None)
            except ValueError:
                return f"{FAILED}: invalid JSON"
        name = parse_eansuche(data)
        # ean-suche.net liefert keine Marke
        return (name, "") if name else None

    async def _opengtindb(self, ean: str) -> tuple[str, str] | str | None:
        params = {"ean": ean, "cmd": "query", "queryid": self.opengtindb_query_id}
        async with self._session.get(
            OPENGTINDB_URL, params=params, headers={"User-Agent": self._headers["User-Agent"]},
            timeout=self._timeout,
        ) as resp:
            if resp.status != 200:
                _LOGGER.warning("OpengtinDB: HTTP %s für %s", resp.status, ean)
                return f"{FAILED}: HTTP {resp.status}"
            raw = await resp.read()
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            body = raw.decode("latin-1")
        error_code, name, vendor = parse_opengtindb(body)
        if error_code == 1:
            return None
        if error_code != 0:
            _LOGGER.warning("OpengtinDB: Fehlercode %s (5 = Limit, 13 = QueryID ungültig)", error_code)
            return f"{FAILED}: error {error_code}"
        if not name:
            return None
        return name, vendor
