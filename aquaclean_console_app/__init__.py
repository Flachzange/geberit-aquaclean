"""One-shot GetFilterStatus escalation diagnostic.

Goal: learn as much as possible from ONE WC power-cycle while staying in the
same BLE session.  Tests become progressively more "SPL-like" and STOP at the
first GetFilterStatus failure.

The production startup has already done its normal first GetFilterStatus and
normal full bridge-order SPL before this suite starts.

Escalation, all in the SAME BLE session:

  E0  control GetFilterStatus
  E1  3 x GetDeviceIdentification, then GetFilterStatus
  E2  GetSPL [0], then GetFilterStatus
  E3  GetSPL [0,1,2,3,4,5,6,7], then GetFilterStatus
  E4  GetSPL [13,12,0,1,2,3,4,5,6,7] (iPhone order), then GetFilterStatus
  E5  GetSPL [0,1,2,3,4,5,6,7,12,13] (bridge order), then GetFilterStatus

Every AquaClean send_request() is logged with one global monotonically
increasing RPC number, including the normal startup and any recovery clients.

At the FIRST 0x59 timeout the escalation stops and recovery D runs:

  D1  wait 10 s, same BLE session, retry 0x59
  D2  BLE-only disconnect/reconnect, same client/connector, retry 0x59
  D3  full transport close + fresh Connector/AquaCleanClient, retry 0x59
  D4  restart ESP32 proxy, wait 15 s + another fresh client, retry 0x59

Production main.py remains unchanged. This module is temporary diagnostic code.
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

_SPL_MINI = [0]
_SPL_8 = [0, 1, 2, 3, 4, 5, 6, 7]
_SPL_IPHONE_10 = [13, 12, 0, 1, 2, 3, 4, 5, 6, 7]
_SPL_BRIDGE_10 = [0, 1, 2, 3, 4, 5, 6, 7, 12, 13]

_orig_send_request = _AquaCleanBaseClient.send_request
_orig_get_filter_status = _AquaCleanBaseClient.get_filter_status_async
_orig_get_common_settings = _AquaCleanBaseClient.get_stored_common_settings_async
_orig_get_spl = _AquaCleanBaseClient.get_system_parameter_list_async
_orig_get_identification = _AquaCleanBaseClient.get_device_identification_async
_orig_client_connect_ble_only = _AquaCleanClient.connect_ble_only
_orig_connector_disconnect_ble_only = _BluetoothLeConnector.disconnect_ble_only
_orig_connector_disconnect = _BluetoothLeConnector.disconnect

_diag_rpc_global_count = 0


def _fmt_byte(value):
    if isinstance(value, int):
        return f"0x{value:02X}"
    if isinstance(value, bytes) and len(value) == 1:
        return f"0x{value[0]:02X}"
    if isinstance(value, str) and len(value) == 1:
        return f"0x{ord(value):02X}"
    return str(value)


def _rpc_snapshot():
    return _diag_rpc_global_count


def _diag_summary(message: str, *args):
    _diag_logger.info("DIAG ESCALATION SUMMARY: " + message, *args)


async def _diag_send_request(self, api_call, send_as_first_cons=False):
    global _diag_rpc_global_count
    _diag_rpc_global_count += 1
    rpc_no = _diag_rpc_global_count

    try:
        attr = api_call.get_api_call_attribute()
        ctx = _fmt_byte(getattr(attr, "context", "?"))
        proc = _fmt_byte(getattr(attr, "procedure", "?"))
    except Exception:
        ctx = "?"
        proc = "?"

    _diag_logger.info(
        "DIAG RPC #%03d: %s ctx=%s proc=%s first_cons=%s",
        rpc_no,
        api_call.__class__.__name__,
        ctx,
        proc,
        bool(send_as_first_cons),
    )

    try:
        result = await _orig_send_request(
            self, api_call, send_as_first_cons=send_as_first_cons
        )
        _diag_logger.info(
            "DIAG RPC #%03d: COMPLETE %s", rpc_no, api_call.__class__.__name__
        )
        return result
    except Exception as exc:
        _diag_logger.warning(
            "DIAG RPC #%03d: FAILED %s — %s: %s",
            rpc_no,
            api_call.__class__.__name__,
            type(exc).__name__,
            exc,
        )
        raise


async def _probe_filter(base_client, label: str) -> bool:
    before = _rpc_snapshot()
    _diag_logger.info(
        "DIAG ESCALATION %s: testing GetFilterStatus 0x59 (RPC count before=%d)",
        label, before
    )
    try:
        await _orig_get_filter_status(base_client)
        _diag_logger.info(
            "DIAG ESCALATION %s: SUCCESS — 0x59 responded (RPC count now=%d)",
            label, _rpc_snapshot()
        )
        return True
    except _BLEPeripheralTimeoutError:
        _diag_logger.warning(
            "DIAG ESCALATION %s: TIMEOUT — 0x59 did not complete (RPC count now=%d)",
            label, _rpc_snapshot()
        )
        return False


async def _probe_spl(base_client, params, label: str) -> bool:
    before = _rpc_snapshot()
    _diag_logger.info(
        "DIAG ESCALATION %s: sending GetSPL params=%s (RPC count before=%d)",
        label, params, before
    )
    try:
        await _orig_get_spl(base_client, params)
        _diag_logger.info(
            "DIAG ESCALATION %s: SUCCESS — GetSPL completed (RPC count now=%d)",
            label, _rpc_snapshot()
        )
        return True
    except _BLEPeripheralTimeoutError:
        _diag_logger.warning(
            "DIAG ESCALATION %s: TIMEOUT — GetSPL itself did not complete (RPC count now=%d)",
            label, _rpc_snapshot()
        )
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
        _diag_logger.warning(
            "DIAG ESCALATION: BLE-only cleanup failed: %s", exc
        )


async def _safe_full_disconnect(connector):
    try:
        await _orig_connector_disconnect(connector)
    except Exception as exc:
        _diag_logger.warning(
            "DIAG ESCALATION: full connector cleanup failed: %s", exc
        )


async def _recovery_suite(connector, client, device_id: str, reason: str):
    """Try to recover 0x59 without power-cycling the WC."""
    base_client = client.base_client

    _diag_logger.warning(
        "DIAG ESCALATION D: recovery cascade starting because %s", reason
    )

    _diag_logger.info(
        "DIAG ESCALATION D1: waiting 10 s in SAME BLE session"
    )
    await _asyncio.sleep(10.0)
    if await _probe_filter(base_client, "D1"):
        _diag_summary(
            "RECOVERED at D1 after 10 s in same BLE session. Failure is transient/timing-related."
        )
        return "D1"

    _diag_logger.info(
        "DIAG ESCALATION D2: BLE-only disconnect/reconnect with SAME client/connector"
    )
    await _safe_ble_disconnect(connector)
    try:
        await _orig_client_connect_ble_only(client, device_id)
        if await _probe_filter(base_client, "D2"):
            _diag_summary(
                "RECOVERED at D2 after BLE-only reconnect. Poisoned state is BLE-session-local."
            )
            return "D2"
    except Exception as exc:
        _diag_logger.warning(
            "DIAG ESCALATION D2: reconnect failed: %s", exc
        )

    _diag_logger.info(
        "DIAG ESCALATION D3: full disconnect + NEW BluetoothLeConnector + NEW AquaCleanClient"
    )
    await _safe_full_disconnect(connector)

    fresh_connector, fresh_client = _new_fresh_client_from(connector)
    try:
        await _orig_client_connect_ble_only(fresh_client, device_id)
        if await _probe_filter(fresh_client.base_client, "D3"):
            _diag_summary(
                "RECOVERED at D3 with fresh Python client/connector. Bridge/transport object state is implicated."
            )
            return "D3"
    except Exception as exc:
        _diag_logger.warning(
            "DIAG ESCALATION D3: fresh-client connect/test failed: %s", exc
        )
    finally:
        await _safe_full_disconnect(fresh_connector)

    esphome_host = getattr(connector, "esphome_host", None)
    if not esphome_host:
        _diag_summary(
            "D1-D3 failed and no ESPHome proxy is configured. Persistent WC-side state is strongly suspected."
        )
        return "NO_ESPHOME"

    _diag_logger.info(
        "DIAG ESCALATION D4: restarting ESP32 proxy; WC remains powered"
    )
    try:
        await connector.restart_esp32_async()
    except Exception as exc:
        _diag_logger.warning(
            "DIAG ESCALATION D4: ESP32 restart command failed: %s", exc
        )
        _diag_summary(
            "D1-D3 failed; D4 restart could not be executed. WC-vs-proxy distinction remains open."
        )
        return "D4_RESTART_FAILED"

    _diag_logger.info(
        "DIAG ESCALATION D4: ESP32 restart sent; waiting 15 s for proxy boot"
    )
    await _asyncio.sleep(15.0)

    fresh2_connector, fresh2_client = _new_fresh_client_from(connector)
    try:
        await _orig_client_connect_ble_only(fresh2_client, device_id)
        if await _probe_filter(fresh2_client.base_client, "D4"):
            _diag_summary(
                "RECOVERED at D4 only after ESP32 restart. ESPHome proxy/transport state is implicated."
            )
            return "D4"
    except Exception as exc:
        _diag_logger.warning(
            "DIAG ESCALATION D4: post-restart connect/test failed: %s", exc
        )
    finally:
        await _safe_full_disconnect(fresh2_connector)

    _diag_summary(
        "D1-D4 all failed. 0x59 survived neither wait, BLE reconnect, fresh Python/ESPHome connection nor ESP32 restart. Persistent state inside the WC is strongly indicated."
    )
    return "WC"


async def _diag_get_filter_status(self):
    result = await _orig_get_filter_status(self)

    # Diagnostic calls use _orig_get_filter_status directly, so only the normal
    # production startup call reaches this wrapper.
    if not getattr(self, "_diag_escalation_initial_filter_seen", False):
        self._diag_escalation_initial_filter_seen = True
        self._diag_escalation_initial_success_at = _time.monotonic()
        _diag_logger.info(
            "DIAG ESCALATION #1: normal startup GetFilterStatus SUCCESS at global RPC #%03d — suite armed",
            _rpc_snapshot(),
        )

    return result


async def _diag_client_connect_ble_only(self, device_id: str):
    result = await _orig_client_connect_ble_only(self, device_id)
    self.base_client._diag_escalation_owner_client = self
    self.base_client._diag_escalation_device_id = device_id
    return result


async def _stop_and_recover(self, connector, owner_client, device_id, reason):
    await _recovery_suite(connector, owner_client, device_id, reason)
    return False


async def _diag_get_common_settings(self):
    result = await _orig_get_common_settings(self)

    armed_at = getattr(self, "_diag_escalation_initial_success_at", None)
    already_started = getattr(self, "_diag_escalation_started", False)

    if (
        armed_at is None
        or already_started
        or (_time.monotonic() - armed_at) >= 60.0
    ):
        return result

    self._diag_escalation_started = True

    owner_client = getattr(self, "_diag_escalation_owner_client", None)
    device_id = getattr(self, "_diag_escalation_device_id", None)
    connector = self.bluetooth_le_connector

    if owner_client is None or not device_id:
        _diag_summary(
            "ABORTED — owner client/device id could not be resolved."
        )
        return result

    _diag_logger.info(
        "DIAG ESCALATION: starting progressive SAME-SESSION ladder at global RPC count=%d",
        _rpc_snapshot(),
    )

    # E0 — control after complete normal startup sequence.
    if not await _probe_filter(self, "E0"):
        await _stop_and_recover(
            connector, owner_client, device_id,
            "E0 control failed before escalation",
        )
        return result

    # E1 — add generic, known-good RPC traffic without SPL.
    _diag_logger.info(
        "DIAG ESCALATION E1: sending 3 x GetDeviceIdentification before next 0x59"
    )
    for i in range(1, 4):
        try:
            await _orig_get_identification(self, 0)
            _diag_logger.info(
                "DIAG ESCALATION E1.%d: GetDeviceIdentification SUCCESS (RPC count now=%d)",
                i, _rpc_snapshot()
            )
        except _BLEPeripheralTimeoutError:
            await _stop_and_recover(
                connector, owner_client, device_id,
                f"E1.{i} GetDeviceIdentification itself timed out",
            )
            return result

    if not await _probe_filter(self, "E1F"):
        await _stop_and_recover(
            connector, owner_client, device_id,
            "E1F 0x59 failed after three extra non-SPL identification RPCs",
        )
        return result

    _diag_summary(
        "E1 PASS — three extra non-SPL RPCs did not kill 0x59; pure call-count/traffic threshold becomes less likely."
    )

    # E2 — minimal repeated 0x0D request.
    if not await _probe_spl(self, _SPL_MINI, "E2"):
        await _stop_and_recover(
            connector, owner_client, device_id,
            "E2 minimal GetSPL [0] itself timed out",
        )
        return result

    if not await _probe_filter(self, "E2F"):
        await _stop_and_recover(
            connector, owner_client, device_id,
            "E2F 0x59 failed after minimal GetSPL [0]; repeated proc 0x0D itself is sufficient",
        )
        return result

    _diag_summary(
        "E2 PASS — repeated GetSPL procedure 0x0D with one parameter does NOT kill 0x59."
    )

    # E3 — eight values; still no meaningful tail IDs 12/13.
    if not await _probe_spl(self, _SPL_8, "E3"):
        await _stop_and_recover(
            connector, owner_client, device_id,
            "E3 eight-parameter GetSPL itself timed out",
        )
        return result

    if not await _probe_filter(self, "E3F"):
        await _stop_and_recover(
            connector, owner_client, device_id,
            "E3F 0x59 failed after GetSPL [0..7]; request/response size up to eight params is sufficient",
        )
        return result

    _diag_summary(
        "E3 PASS — GetSPL [0..7] does NOT kill 0x59. Any later failure is tied to the 10-param form/order/tail."
    )

    # E4 — exact parameter set in observed iPhone order.
    if not await _probe_spl(self, _SPL_IPHONE_10, "E4"):
        await _stop_and_recover(
            connector, owner_client, device_id,
            "E4 iPhone-order ten-parameter GetSPL itself timed out",
        )
        return result

    if not await _probe_filter(self, "E4F"):
        await _stop_and_recover(
            connector, owner_client, device_id,
            "E4F 0x59 failed after iPhone-order 10-param GetSPL; ten-param/12+13 behavior is sufficient even with iPhone order",
        )
        return result

    _diag_summary(
        "E4 PASS — iPhone-order 10-param SPL preserves 0x59. Bridge order / placement of 12+13 becomes the prime suspect."
    )

    # E5 — current bridge order. Deliberately LAST because this was already shown
    # to be destructive in the previous run.
    if not await _probe_spl(self, _SPL_BRIDGE_10, "E5"):
        await _stop_and_recover(
            connector, owner_client, device_id,
            "E5 bridge-order ten-parameter GetSPL itself timed out",
        )
        return result

    if not await _probe_filter(self, "E5F"):
        await _stop_and_recover(
            connector, owner_client, device_id,
            "E5F 0x59 failed after bridge-order 10-param SPL while iPhone order survived; ordering/CONS placement is strongly implicated",
        )
        return result

    _diag_summary(
        "E0-E5 ALL PASS — generic RPC traffic, repeated proc 0x0D, 1-param, 8-param, iPhone 10-param and bridge 10-param SPL all preserved 0x59. The earlier failure is non-deterministic or depends on another state/timing variable."
    )

    return result


_AquaCleanBaseClient.send_request = _diag_send_request
_AquaCleanBaseClient.get_filter_status_async = _diag_get_filter_status
_AquaCleanBaseClient.get_stored_common_settings_async = _diag_get_common_settings
_AquaCleanClient.connect_ble_only = _diag_client_connect_ble_only
