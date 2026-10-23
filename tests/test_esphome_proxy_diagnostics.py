"""Regression tests for ESPHome proxy telemetry without BLE or MQTT dependencies.

Run: python -m unittest discover -s tests -p test_esphome_proxy_diagnostics.py
"""
import ast
import asyncio
import logging
import math
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parent.parent


def method_source(path, first, next_method):
    text = (ROOT / path).read_text(encoding="utf-8")
    start = text.index("    async def " + first + "(")
    end = text.index("    async def " + next_method + "(", start)
    return text[start:end]


diag_methods = method_source(
    "aquaclean_console_app/bluetooth_le/LE/BluetoothLeConnector.py",
    "_read_esphome_diagnostics_async", "_invalidate_esphome_api"
)
diag_ns = {
    "asyncio": asyncio, "math": math, "datetime": datetime, "timezone": timezone,
    "logger": logging.getLogger(__name__),
}
ast.parse("class Connector:\n" + diag_methods)
exec("class Connector:\n" + diag_methods, diag_ns)
Connector = diag_ns["Connector"]


class FakeAPI:
    def __init__(self, entries, states):
        self.list_entities_services = AsyncMock(
            return_value=([types.SimpleNamespace(key=k, object_id=oid)
                           for k, oid in entries], [])
        )
        self.states = states
        self.unsubscribed = False

    def subscribe_states(self, callback):
        for k, value in self.states.items():
            callback(types.SimpleNamespace(key=k, state=value))

        def unsubscribe():
            self.unsubscribed = True
        return unsubscribe


class ConnectorTests(unittest.IsolatedAsyncioTestCase):
    def connector(self, api):
        result = Connector()
        result._esphome_api = api
        result._esphome_diagnostic_keys = None
        result.esphome_diagnostics_timestamp = None
        for field in ("wifi_rssi", "free_heap", "max_free_block", "min_free_heap",
                      "uptime", "reset_reason", "bssid"):
            setattr(result, "esphome_" + field, None)
        return result

    async def test_all_diagnostics_and_exact_heap_mapping(self):
        entries = [
            (1, "aquaclean_proxy_min_free_heap"),
            (2, "aquaclean_proxy_free_heap"),
            (3, "aquaclean_proxy_max_free_block"),
            (4, "aquaclean_proxy_uptime"),
            (5, "aquaclean_proxy_wifi_rssi"),
            (6, "aquaclean_proxy_reset_reason"),
            (7, "aquaclean_proxy_connected_bssid"),
        ]
        states = {
            1: 120000.0, 2: 135000.0, 3: 95000.0, 4: 125.9,
            5: -57.6, 6: "SW_CPU_RESET", 7: "AA:BB:CC:DD:EE:FF"
        }
        api = FakeAPI(entries, states)
        connector = self.connector(api)
        await connector._read_esphome_diagnostics_async()
        self.assertEqual(connector.esphome_min_free_heap, 120000)
        self.assertEqual(connector.esphome_free_heap, 135000)
        self.assertEqual(connector.esphome_max_free_block, 95000)
        self.assertEqual(connector.esphome_uptime, 125)
        self.assertEqual(connector.esphome_wifi_rssi, -57.6)
        self.assertEqual(connector.esphome_reset_reason, "SW_CPU_RESET")
        self.assertEqual(connector.esphome_bssid, "AA:BB:CC:DD:EE:FF")
        self.assertTrue(connector.esphome_diagnostics_timestamp.endswith("Z"))
        self.assertTrue(api.unsubscribed)
        await connector._read_esphome_diagnostics_async()
        api.list_entities_services.assert_awaited_once()

    async def test_missing_optional_sensors_do_not_fail(self):
        api = FakeAPI([(1, "aquaclean_proxy_uptime")], {1: 30.0})
        connector = self.connector(api)
        await connector._read_esphome_diagnostics_async()
        self.assertEqual(connector.esphome_uptime, 30)
        self.assertIsNone(connector.esphome_reset_reason)
        self.assertIsNone(connector.esphome_min_free_heap)

    async def test_failed_snapshot_does_not_mark_fresh(self):
        api = FakeAPI([], {})
        api.list_entities_services = AsyncMock(side_effect=OSError("unavailable"))
        connector = self.connector(api)
        await connector._read_esphome_diagnostics_async()
        self.assertIsNone(connector.esphome_diagnostics_timestamp)


class FakeMQTT:
    def __init__(self):
        self.sent = []

    async def send_data_async(self, topic, value):
        self.sent.append((topic, value))


publisher_methods = method_source(
    "aquaclean_console_app/main.py",
    "_publish_esphome_proxy_status", "_publish_esphome_proxy_discovery"
)
publisher_ns = {}
ast.parse("class Publisher:\n" + publisher_methods)
exec("class Publisher:\n" + publisher_methods, publisher_ns)
Publisher = publisher_ns["Publisher"]


class PublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_and_new_proxy_topics(self):
        pub = Publisher()
        pub.mqttConfig = {"topic": "Geberit/AquaClean"}
        pub.mqtt_service = FakeMQTT()
        pub._last_published_esphome_error_code = "E0000"
        pub.esphome_proxy_state = {
            "enabled": True, "connected": True, "name": "proxy",
            "host": "192.168.30.22", "port": 6053,
            "error_code": "E0000", "wifi_rssi": -50.0,
            "free_heap": 135000, "max_free_block": 95000,
            "min_free_heap": 120000, "uptime": 125,
            "reset_reason": "SW_CPU_RESET", "bssid": "AA:BB:CC:DD:EE:FF",
            "diagnostics_updated_at": "2026-10-23T08:00:00Z",
        }
        await pub._publish_esphome_proxy_status()
        topics = dict(pub.mqtt_service.sent)
        prefix = "Geberit/AquaClean/esphomeProxy/"
        for suffix in (
            "wifiRssi", "freeHeap", "maxFreeBlock", "minFreeHeap",
            "uptime", "resetReason", "bssid", "diagnosticsUpdatedAt"
        ):
            self.assertIn(prefix + suffix, topics)
        self.assertEqual(topics[prefix + "uptime"], "125")
        self.assertEqual(topics[prefix + "minFreeHeap"], "120000")

    def test_sampled_from_watchdog_not_ble_poll(self):
        mode_src = (ROOT / "aquaclean_console_app/main.py").read_text()
        ble_src = (ROOT / "aquaclean_console_app/bluetooth_le/LE/BluetoothLeConnector.py").read_text()
        watchdog = mode_src[
            mode_src.index("    async def _probe_esphome_proxy("):
            mode_src.index("    async def _set_esphome_api_health(")
        ]
        ble_connection = ble_src[
            ble_src.index("    async def _connect_via_esphome("):
            ble_src.index("    async def ", ble_src.index("    async def _connect_via_esphome(") + 1)
        ]
        self.assertIn("await connector._read_esphome_diagnostics_async()", watchdog)
        self.assertNotIn("_read_esphome_diagnostics_async", ble_connection)


if __name__ == "__main__":
    unittest.main(verbosity=2)
