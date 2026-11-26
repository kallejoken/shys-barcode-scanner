"""Sensoren: letzter Scan, bekannte Produkte, offene unbekannte Codes."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NAME, SIGNAL_UPDATE, VERSION
from .manager import BarcodeManager

MAX_PENDING_ATTR = 50


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager: BarcodeManager = hass.data[DOMAIN]
    async_add_entities(
        [
            LastScanSensor(manager, entry),
            KnownProductsSensor(manager, entry),
            UnknownCountSensor(manager, entry),
        ]
    )


class BarcodeSensorBase(SensorEntity):
    """Gemeinsame Basis."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _key: str = ""

    def __init__(self, manager: BarcodeManager, entry: ConfigEntry) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_{self._key}"
        self._attr_translation_key = self._key
        # Feste, gut lesbare Entity-IDs (z. B. sensor.shys_barcode_scanner_last_scan)
        self.entity_id = f"sensor.{DOMAIN}_{self._key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title or NAME,
            manufacturer="SmartHome-yourself",
            model=NAME,
            sw_version=VERSION,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class LastScanSensor(BarcodeSensorBase):
    _key = "last_scan"
    _attr_icon = "mdi:barcode-scan"

    @property
    def native_value(self) -> str | None:
        result = self._manager.last_result
        if not result:
            return None
        value = result.get("name") or result.get("ean") or ""
        return value[:255] or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._manager.last_result or {}
        return {
            "ean": result.get("ean"),
            "brand": result.get("brand"),
            "product": result.get("product"),
            "source": result.get("source"),
            "status": result.get("status"),
            "action": result.get("action"),
            "target_list": result.get("target_list"),
            "device": result.get("device"),
            "timestamp": result.get("timestamp"),
        }


class KnownProductsSensor(BarcodeSensorBase):
    _key = "known_products"
    _attr_icon = "mdi:database"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "Produkte"

    @property
    def native_value(self) -> int:
        return len(self._manager.storage.products)


class UnknownCountSensor(BarcodeSensorBase):
    _key = "unknown_count"
    _attr_icon = "mdi:barcode-off"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int:
        return len(self._manager.storage.pending)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pending = self._manager.storage.list_pending()[:MAX_PENDING_ATTR]
        return {"pending": [p["ean"] for p in pending]}
