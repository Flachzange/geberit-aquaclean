"""One-shot GetFilterStatus / GetSPL state-machine diagnostic.

Goal: extract as much information as possible from ONE WC power-cycle.

The suite runs automatically during the first on-demand startup session after
GetFilterStatus succeeds:

  A) SAME BLE SESSION
     - control GetFilterStatus
     - repeat bridge-order SPL [0..7,12,13] in the same BLE session
     - GetFilterStatus again

  B) NEW BLE SESSION, IPHONE SPL ORDER
     - disconnect/reconnect + normal 0x11/0x13 subscription sequence
     - control GetFilterStatus
     - SPL [13,12,0,1,2,3,4,5,6,7]
     - GetFilterStatus again

  C) NEW BLE SESSION, BRIDGE SPL ORDER
     - disconnect/reconnect + normal 0x11/0x13 subscription sequence
     - control GetFilterStatus
     - SPL [0,1,2,3,4,5,6,7,12,13]
     - GetFilterStatus again

If any stage wedges 0x59, recovery D runs immediately:
  D1) wait 10 s, retry in same BLE session
  D2) BLE-only disconnect/reconnect using the same client/connector
  D3) full transport close + completely fresh Connector/AquaCleanClient
  D4) restart ESP32 proxy, wait, then use another fresh Connector/AquaCleanClient

Production main.py remains unchanged. This file is temporary diagnostic code only.
"""

import asyncio as _asyncio
import logging as _logging
import time as _time

from aquaclean_console_app.aquaclean_core.Clients.AquaCleanBaseClient import (
    AquaCleanBaseClient as _AquaCleanBaseClient,
    BLEPeripheralTimeoutError as _BLEPeripheralTimeoutError,
)
from aquaclean_console_app.aquaclean_core.Clients.AquaCleanClient import (
    AquaCleanClient as _AquaCleanClient,
)
from aquaclean_console_app.bluetooth_le.LE.BluetoothLeConnector import (
    BluetoothLeConnector as _BluetoothLeConnector,
)

_diag_logger = _logging.getLogger("aquaclean_console_app.main")

_BRIDGE_SPL = [0, 1, 2, 3, 4, 5, 6, 7, 12, 13]
_IPHONE_SPL = [13, 12, 0, 1, 2, 3, 4, 5, 6, 7]

_orig_get_filter_status = _AquaCleanBaseClient.get_filter_status_async
_orig_get_common_settings = _AquaCleanBaseClient.get_stored_common_settings_async
_orig_get_spl = _AquaCleanBaseClient.get_system_parameter_list_async
_orig_client_connect_ble_only = _AquaCleanClient.connect_ble_only
_orig_connector_disconnect_ble_only = _BluetoothLeConnector.disconnect_ble_only
_orig_connector_disconnect = _BluetoothLeConnector.disconnect

_diag_clients_by_connector = {}


def _diag_summary(message: str, *args):
    _diag_logger.info("DIAG SUITE SUMMARY: " + message, *args)


async def _probe_filter(base_client, label: str) -> bool:
    _diag_logger.info("DIAG SUITE %s: testing GetFilterStatus 0x59", label)
    try:
        await _orig_get_filter_status(base_client)
        _diag_logger.info("DIAG SUITE %s: SUCCESS — 0x59 responded", label)
        return True
    except _BLEPeripheralTimeoutError:
        _diag_logger.warning("DIAG SUITE %s: TIMEOUT — 0x59 did not complete", label)
        return False


async def _probe_spl(base_client, params, label: str) -> bool:
    _diag_logger.info("DIAG SUITE %s: sending GetSPL params=%s", label, params)
    try:
        await _orig_get_spl(base_client, params)
        _diag_logger.info("DIAG SUITE %s: SUCCESS — GetSPL completed", label)
        return True
    except _BLEPeripheralTimeoutError:
        _diag_logger.warning("DIAG SUITE %s: TIMEOUT — GetSPL itself did not complete", label)
        return False


def _new_fresh_client_from(connector):
    fresh_connector = _BluetoothLeConnector(
        esphome_host=getattr(connector, "esphome_host", None),
        esphome_port=getattr(connector, "esphome_port", 6053),
        esphome_noise_psk=getattr(connector, "esphome_noise_psk", None),
        hass=getattr(connector, "_hass", None),
    )
    return fresh_connector, _AquaCleanClient(fresh_connector)


async def _safe_ble_disconnect(connector):
    try:
        await _orig_connector_disconnect_ble_only(connector)
    except Exception as exc:
        _diag_logger.warning("DIAG SUITE: BLE-only cleanup failed: %s", exc)


async def _safe_full_disconnect(connector):
    try:
        await _orig_connector_disconnect(connector)
    except Exception as exc:
        _diag_logger.warning("DIAG SUITE: full connector cleanup failed: %s", exc)


async def _recovery_suite(connector, client, device_id: str, reason: str):
    """Try to recover 0x59 without power-cycling the WC."""
    base_client = client.base_client

    _diag_logger.warning(
        "DIAG SUITE D: recovery cascade starting because %s", reason
    )

    # D1 — distinguish a transient post-SPL quiet-time problem from persistent state.
    _diag_logger.info(
        "DIAG SUITE D1: waiting 10 s in the SAME BLE session before retrying 0x59"
    )
    await _asyncio.sleep(10.0)
    if await _probe_filter(base_client, "D1"):
        _diag_summary(
            "RECOVERED after 10 s in same BLE session — failure is transient/timing-related, not persistent."
        )
        return "D1"

    # D2 — same Python objects / same persistent ESPHome API, fresh BLE session.
    _diag_logger.info(
        "DIAG SUITE D2: BLE-only disconnect/reconnect with SAME client/connector"
    )
    await _safe_ble_disconnect(connector)
    try:
        await _orig_client_connect_ble_only(client, device_id)
        if await _probe_filter(base_client, "D2"):
            _diag_summary(
                "RECOVERED after BLE-only reconnect — poisoned state is session-local; ESPHome API/Python objects can stay alive."
            )
            return "D2"
    except Exception as exc:
        _diag_logger.warning("DIAG SUITE D2: reconnect failed: %s", exc)

    # D3 — close the persistent API transport and throw away all protocol/parser objects.
    _diag_logger.info(
        "DIAG SUITE D3: full disconnect, then NEW BluetoothLeConnector + NEW AquaCleanClient"
    )
    await _safe_full_disconnect(connector)

    fresh_connector, fresh_client = _new_fresh_client_from(connector)
    try:
        await _orig_client_connect_ble_only(fresh_client, device_id)
        if await _probe_filter(fresh_client.base_client, "D3"):
            _diag_summary(
                "RECOVERED with completely fresh Python client/connector — state was in bridge/transport objects, not persistently in the WC."
            )
            return "D3"
    except Exception as exc:
        _diag_logger.warning("DIAG SUITE D3: fresh-client connect/test failed: %s", exc)
    finally:
        await _safe_full_disconnect(fresh_connector)

    # D4 — now separate ESP32/proxy state from WC state.
    esphome_host = getattr(connector, "esphome_host", None)
    if not esphome_host:
        _diag_summary(
            "D1-D3 failed and no ESPHome proxy is configured — persistent WC-side state is strongly suspected."
        )
        return "NO_ESPHOME"

    _diag_logger.info(
        "DIAG SUITE D4: restarting ESP32 proxy (WC remains powered), then testing with another fresh client"
    )
    try:
        await connector.restart_esp32_async()
    except Exception as exc:
        _diag_logger.warning("DIAG SUITE D4: ESP32 restart command failed: %s", exc)
        _diag_summary(
            "D1-D3 failed; ESP32 restart could not be executed. WC-vs-proxy distinction remains open."
        )
        return "D4_RESTART_FAILED"

    _diag_logger.info("DIAG SUITE D4: ESP32 restart sent; waiting 15 s for proxy boot")
    await _asyncio.sleep(15.0)

    fresh2_connector, fresh2_client = _new_fresh_client_from(connector)
    try:
        await _orig_client_connect_ble_only(fresh2_client, device_id)
        if await _probe_filter(fresh2_client.base_client, "D4"):
            _diag_summary(
                "RECOVERED only after ESP32 restart — ESPHome proxy/transport state is implicated; WC power-cycle is NOT required."
            )
            return "D4"
    except Exception as exc:
        _diag_logger.warning("DIAG SUITE D4: post-restart connect/test failed: %s", exc)
    finally:
        await _safe_full_disconnect(fresh2_connector)

    _diag_summary(
        "D1-D4 all failed — 0x59 survives wait, BLE reconnect, fresh Python/ESPHome connection, and ESP32 restart. Persistent state inside the WC is now strongly indicated; WC power-cycle remains the recovery."
    )
    return "WC"


async def _diag_get_filter_status(self):
    result = await _orig_get_filter_status(self)

    # Only the first successful production GetFilterStatus after process start arms
    # the one-shot suite. Diagnostic probes call _orig_get_filter_status directly.
    if not getattr(self, "_diag_suite_initial_filter_seen", False):
        self._diag_suite_initial_filter_seen = True
        self._diag_suite_initial_success_at = _time.monotonic()
        _diag_logger.info(
            "DIAG SUITE #1: normal startup GetFilterStatus SUCCESS — one-shot suite armed"
        )

    return result


async def _diag_client_connect_ble_only(self, device_id: str):
    result = await _orig_client_connect_ble_only(self, device_id)
    connector = self.base_client.bluetooth_le_connector
    _diag_clients_by_connector[id(connector)] = (self, device_id)
    self.base_client._diag_suite_owner_client = self
    self.base_client._diag_suite_device_id = device_id
    return result


async def _diag_get_common_settings(self):
    result = await _orig_get_common_settings(self)

    armed_at = getattr(self, "_diag_suite_initial_success_at", None)
    already_started = getattr(self, "_diag_suite_started", False)

    if (
        armed_at is None
        or already_started
        or (_time.monotonic() - armed_at) >= 60.0
    ):
        return result

    owner_client = getattr(self, "_diag_suite_owner_client", None)
    device_id = getattr(self, "_diag_suite_device_id", None)
    connector = self.bluetooth_le_connector

    if owner_client is None or not device_id:
        _diag_logger.warning(
            "DIAG SUITE: startup probe armed but owner client/device id could not be resolved; suite aborted"
        )
        self._diag_suite_started = True
        self._diag_suite_after_disconnect_done = True
        return result

    self._diag_suite_started = True

    _diag_logger.info(
        "DIAG SUITE A: SAME-SESSION test begins — no BLE disconnect has occurred"
    )

    # A0: old #2 control point. Confirms that the complete startup sequence itself
    # has not already killed 0x59.
    if not await _probe_filter(self, "A0"):
        self._diag_suite_after_disconnect_done = True
        await _recovery_suite(
            connector, owner_client, device_id,
            "A0 control failed before the repeated same-session SPL",
        )
        return result

    # Small quiet time so the test is not merely measuring two immediate back-to-back
    # transactions after the startup common-settings burst.
    await _asyncio.sleep(1.0)

    # A1/A2: same connection, same subscription registration, second bridge-order SPL.
    if not await _probe_spl(self, _BRIDGE_SPL, "A1"):
        self._diag_suite_after_disconnect_done = True
        await _recovery_suite(
            connector, owner_client, device_id,
            "A1 second bridge-order SPL timed out in the same BLE session",
        )
        return result

    if not await _probe_filter(self, "A2"):
        self._diag_suite_after_disconnect_done = True
        await _recovery_suite(
            connector, owner_client, device_id,
            "A2 0x59 failed after a second bridge-order SPL in the SAME BLE session",
        )
        return result

    self._diag_suite_phase_a_success = True
    _diag_summary(
        "A PASS — a second bridge-order SPL in the SAME BLE session does NOT kill 0x59. Next: session-change tests B/C."
    )
    return result


async def _run_session_test(client, device_id: str, params, phase: str, description: str):
    base_client = client.base_client
    connector = base_client.bluetooth_le_connector

    _diag_logger.info(
        "DIAG SUITE %s: NEW BLE session — %s", phase, description
    )
    try:
        await _orig_client_connect_ble_only(client, device_id)
    except Exception as exc:
        _diag_logger.warning("DIAG SUITE %s0: BLE reconnect failed: %s", phase, exc)
        return False, "connect"

    if not await _probe_filter(base_client, f"{phase}0"):
        return False, "control"

    await _asyncio.sleep(0.5)

    if not await _probe_spl(base_client, params, f"{phase}1"):
        return False, "spl"

    if not await _probe_filter(base_client, f"{phase}2"):
        return False, "post"

    _diag_logger.info(
        "DIAG SUITE %s: PASS — 0x59 survived this session/SPL combination", phase
    )
    return True, None


async def _diag_disconnect_ble_only(self):
    entry = _diag_clients_by_connector.get(id(self))
    run_bc = False
    client = None
    device_id = None
    base_client = None

    if entry is not None:
        client, device_id = entry
        base_client = client.base_client
        run_bc = (
            getattr(base_client, "_diag_suite_phase_a_success", False)
            and not getattr(base_client, "_diag_suite_after_disconnect_done", False)
        )
        if run_bc:
            # Arm before the production disconnect so nested/cleanup calls cannot
            # start the suite twice.
            base_client._diag_suite_after_disconnect_done = True

    # Always perform the production BLE-only disconnect first.
    await _orig_connector_disconnect_ble_only(self)

    if not run_bc:
        return

    try:
        # B — iPhone order. This is the same 10 parameters but with 13/12 in the
        # FIRST frame instead of at the tail/CONS part used by the bridge order.
        ok, fail_stage = await _run_session_test(
            client,
            device_id,
            _IPHONE_SPL,
            "B",
            "iPhone SPL order [13,12,0,1,2,3,4,5,6,7]",
        )
        if not ok:
            await _recovery_suite(
                self, client, device_id,
                f"B failed at {fail_stage}: new session + iPhone-order SPL path",
            )
            return

        # B succeeded. Disconnect cleanly before creating C.
        await _safe_ble_disconnect(self)

        # C — exact current bridge order.
        ok, fail_stage = await _run_session_test(
            client,
            device_id,
            _BRIDGE_SPL,
            "C",
            "bridge SPL order [0,1,2,3,4,5,6,7,12,13]",
        )
        if not ok:
            await _recovery_suite(
                self, client, device_id,
                f"C failed at {fail_stage}: new session + bridge-order SPL path",
            )
            return

        _diag_summary(
            "A+B+C ALL PASS — same-session repeat, new-session iPhone order, and new-session bridge order all preserve 0x59. The previous failure is therefore non-deterministic or depends on timing/state not reproduced by this one-shot sequence."
        )

    except Exception as exc:
        _diag_logger.exception("DIAG SUITE: unexpected exception in B/C suite: %s", exc)
    finally:
        # Leave the production connector in the state expected by on-demand mode.
        await _safe_ble_disconnect(self)


_AquaCleanBaseClient.get_filter_status_async = _diag_get_filter_status
_AquaCleanBaseClient.get_stored_common_settings_async = _diag_get_common_settings
_AquaCleanClient.connect_ble_only = _diag_client_connect_ble_only
_BluetoothLeConnector.disconnect_ble_only = _diag_disconnect_ble_only
