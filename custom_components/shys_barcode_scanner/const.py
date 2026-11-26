"""Konstanten für die Integration SHYS Barcode Scanner."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "shys_barcode_scanner"
NAME: Final = "SHYS Barcode Scanner"
VERSION: Final = "1.0.0"

# Lokale Datenbank (HA-Store unter <config>/.storage/shys_barcode_scanner)
STORAGE_KEY: Final = DOMAIN
STORAGE_VERSION: Final = 1

# Event, das die ESPHome-Firmware (v3) bei jedem Scan feuert
EVENT_BARCODE_SCANNED: Final = "esphome.barcode_scanned"
# Event, das die Integration nach jeder Verarbeitung feuert (für Automationen)
EVENT_SCAN_PROCESSED: Final = f"{DOMAIN}_scan_processed"

# Dispatcher-Signal für Sensoren
SIGNAL_UPDATE: Final = f"{DOMAIN}_update"

# Panel
PANEL_URL_PATH: Final = "shys-barcode-scanner"
PANEL_COMPONENT: Final = "shys-barcode-panel"
PANEL_TITLE: Final = "Barcode Scanner"
PANEL_ICON: Final = "mdi:barcode-scan"
PANEL_STATIC_URL: Final = f"/{DOMAIN}_static"
PANEL_JS_FILE: Final = "shys-barcode-panel.js"

# Config-/Options-Schlüssel
CONF_NAME: Final = "name"
CONF_TODO_ENTITY: Final = "todo_entity"
CONF_DUPLICATE_MODE: Final = "duplicate_mode"
CONF_UNKNOWN_MODE: Final = "unknown_mode"
CONF_NAME_FORMAT: Final = "name_format"
CONF_USE_OPENFOODFACTS: Final = "use_openfoodfacts"
CONF_USE_EANSUCHE: Final = "use_eansuche"
CONF_OPENGTINDB_QUERY_ID: Final = "opengtindb_query_id"
CONF_USER_AGENT: Final = "user_agent"
CONF_NOTIFY_SERVICE: Final = "notify_service"
CONF_ESPHOME_DEVICE_PREFIX: Final = "esphome_device_prefix"

# Duplikat-Verhalten
DUPLICATE_ALLOW: Final = "allow"
DUPLICATE_SKIP: Final = "skip"
DUPLICATE_INCREMENT: Final = "increment"
DUPLICATE_MODES: Final = [DUPLICATE_ALLOW, DUPLICATE_SKIP, DUPLICATE_INCREMENT]

# Verhalten bei unbekannten Codes
UNKNOWN_PENDING_ONLY: Final = "pending_only"
UNKNOWN_ADD_EAN_AND_PENDING: Final = "add_ean_and_pending"
UNKNOWN_MODES: Final = [UNKNOWN_PENDING_ONLY, UNKNOWN_ADD_EAN_AND_PENDING]

# Namensformat auf der Liste
NAME_FORMAT_BRAND_PRODUCT: Final = "brand_product"
NAME_FORMAT_PRODUCT_ONLY: Final = "product_only"
NAME_FORMATS: Final = [NAME_FORMAT_BRAND_PRODUCT, NAME_FORMAT_PRODUCT_ONLY]

DEFAULT_NAME: Final = "Barcode Scanner"
DEFAULT_TODO_ENTITY: Final = "todo.shopping_list"
DEFAULT_USER_AGENT: Final = (
    "SHYSBarcodeScanner/1.0 (Home Assistant; "
    "https://github.com/SmartHome-yourself/barcode-scanner-for-esphome)"
)

DEFAULT_OPTIONS: Final = {
    CONF_DUPLICATE_MODE: DUPLICATE_INCREMENT,
    CONF_UNKNOWN_MODE: UNKNOWN_PENDING_ONLY,
    CONF_NAME_FORMAT: NAME_FORMAT_BRAND_PRODUCT,
    CONF_USE_OPENFOODFACTS: True,
    CONF_USE_EANSUCHE: True,
    CONF_OPENGTINDB_QUERY_ID: "",
    CONF_USER_AGENT: DEFAULT_USER_AGENT,
    CONF_NOTIFY_SERVICE: "",
    CONF_ESPHOME_DEVICE_PREFIX: "",
}

# Datenquellen
SOURCE_OPENFOODFACTS: Final = "openfoodfacts"
SOURCE_EANSUCHE: Final = "ean-suche"
SOURCE_OPENGTINDB: Final = "opengtindb"
SOURCE_MANUAL: Final = "manual"
SOURCE_IMPORT: Final = "import"

# Scan-Status (geht auch an die ESPHome-Aktion show_result)
STATUS_OK: Final = "ok"
STATUS_UNKNOWN: Final = "unknown"
STATUS_DUPLICATE: Final = "duplicate"
STATUS_ERROR: Final = "error"

# Was mit der Liste passiert ist
ACTION_ADDED: Final = "added"
ACTION_INCREMENTED: Final = "incremented"
ACTION_SKIPPED: Final = "skipped"
ACTION_NONE: Final = "none"

# Service-Namen
SERVICE_SCAN: Final = "scan"
SERVICE_LEARN_PRODUCT: Final = "learn_product"
SERVICE_DELETE_PRODUCT: Final = "delete_product"
SERVICE_DISMISS_PENDING: Final = "dismiss_pending"
SERVICE_EXPORT_DATABASE: Final = "export_database"
SERVICE_IMPORT_DATABASE: Final = "import_database"

ATTR_EAN: Final = "ean"
ATTR_DEVICE: Final = "device"
ATTR_NAME: Final = "name"
ATTR_BRAND: Final = "brand"
ATTR_TARGET_LIST: Final = "target_list"
ATTR_ADD_TO_LIST: Final = "add_to_list"
ATTR_DATA: Final = "data"
ATTR_MERGE: Final = "merge"

# Maximale Länge eines Codes (QR-Codes dürfen länger sein als EANs)
MAX_CODE_LENGTH: Final = 128
