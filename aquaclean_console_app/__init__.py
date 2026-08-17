"""One-shot GetFilterStatus boundary + quiet-time diagnostic.

Goal: maximize information from ONE WC power-cycle.

The bridge performs its normal startup first, including its first 10-param
bridge-order GetSPL.  This suite then runs in the SAME BLE session and stops
at the first failure.

Boundary / parameter isolation:
  F0  GetFilterStatus control
  F1  9 params, safe duplicate:
      GetSPL [0,1,2,3,4,5,6,7,4] -> immediate 0x59
      (first meaningful request byte beyond the 8-param boundary)
  F2  10 params, safe duplicates:
      GetSPL [0,1,2,3,4,5,6,7,4,5] -> immediate 0x59
      (same response record count as the problematic request, no 12/13)
  F3  10 params, ONLY param 12 special:
      GetSPL [12,0,1,2,3,4,5,6,7,4] -> immediate 0x59
  F4  10 params, ONLY param 13 special:
      GetSPL [13,0,1,2,3,4,5,6,7,4] -> immediate 0x59

If F1-F4 all pass, test the known critical 12+13 combination in iPhone order:
  Q1  GetSPL [13,12,0,1,2,3,4,5,6,7]
      WAIT 10 s with NO AquaClean RPC
      then 0x59

If Q1 passes, the large SPL alone did not permanently poison 0x59. Then:
  Q2  same full SPL
      IMMEDIATELY GetDeviceIdentification
      then 0x59
      (tests whether *any* immediate next RPC is rejected / whether another RPC
       closes or resets the state)
  Q3  same full SPL
      WAIT 1 s with NO AquaClean RPC
      then 0x59
      (coarse timing threshold)
  Q4  same full SPL
      IMMEDIATELY 0x59
      (final reproduction control; deliberately last)

Every AquaClean send_request() gets one global RPC number.

At the first failure, escalation stops and recovery D runs:
  D1 wait 10 s, same BLE session, retry 0x59
  D2 BLE-only reconnect, same objects, retry 0x59
  D3 full transport close + fresh Connector/AquaCleanClient, retry 0x59
  D4 restart ESP32 proxy + fresh client, retry 0x59

Production main.py remains unchanged. Temporary diagnostic shim only.
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

_SPL_F1_9_SAFE = [0, 1, 2, 3, 4, 5, 6, 7, 4]
_SPL_F2_10_SAFE = [0, 1, 2, 3, 4, 5, 6, 7, 4, 5]
_SPL_F3_10_PARAM12 = [12, 0, 1, 2, 3, 4, 5, 6, 7, 4]
_SPL_F4_10_PARAM13 = [13, 0, 1, 2, 3, 4, 5, 6, 7, 4]
_SPL_FULL_IPHONE = [13, 12, 0, 1, 2, 3, 4, 5, 6, 7]

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


def _summary(message: str, *args):
    _diag_logger.info("DIAG BOUNDARY SUMMARY: " + message, *args)


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
    _diag_logger.info(
        "DIAG BOUNDARY %s: testing GetFilterStatus 0x59 (RPC before=%d)",
        label, _rpc_snapshot()
    )
    try:
        await _orig_get_filter_status(base_client)
        _diag_logger.info(
            "DIAG BOUNDARY %s: SUCCESS — 0x59 responded (RPC now=%d)",
            label, _rpc_snapshot()
        )
        return True
    except _BLEPeripheralTimeoutError:
        _diag_logger.warning(
            "DIAG BOUNDARY %s: TIMEOUT — 0x59 did not complete (RPC now=%d)",
            label, _rpc_snapshot()
        )
        return False


async def _probe_spl(base_client, params, label: str) -> bool:
    _diag_logger.info(
        "DIAG BOUNDARY %s: GetSPL params=%s (RPC before=%d)",
        label, params, _rpc_snapshot()
    )
    try:
        result = await _orig_get_spl(base_client, params)
        _diag_logger.info(
            "DIAG BOUNDARY %s: SUCCESS — GetSPL completed (RPC now=%d)",
            label, _rpc_snapshot()
        )
        return True
    except _BLEPeripheralTimeoutError:
        _diag_logger.warning(
            "DIAG BOUNDARY %s: TIMEOUT — GetSPL itself did not complete (RPC now=%d)",
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
        _diag_logger.warning("DIAG BOUNDARY: BLE-only cleanup failed: %s", exc)


async def _safe_full_disconnect(connector):
    try:
        await _orig_connector_disconnect(connector)
    except Exception as exc:
        _diag_logger.warning("DIAG BOUNDARY: full connector cleanup failed: %s", exc)


async def _recovery_suite(connector, client, device_id: str, reason: str):
    base_client = client.base_client

    _diag_logger.warning(
        "DIAG BOUNDARY D: recovery cascade starts because %s", reason
    )

    _diag_logger.info("DIAG BOUNDARY D1: wait 10 s in SAME BLE session")
    await _asyncio.sleep(10.0)
    if await _probe_filter(base_client, "D1"):
        _summary(
            "RECOVERED D1 — 10 s after the failed request in the same BLE session restored 0x59."
        )
        return "D1"

    _diag_logger.info(
        "DIAG BOUNDARY D2: BLE-only disconnect/reconnect with SAME client/connector"
    )
    await _safe_ble_disconnect(connector)
    try:
        await _orig_client_connect_ble_only(client, device_id)
        if await _probe_filter(base_client, "D2"):
            _summary("RECOVERED D2 — BLE session-local state.")
            return "D2"
    except Exception as exc:
        _diag_logger.warning("DIAG BOUNDARY D2: reconnect failed: %s", exc)

    _diag_logger.info(
        "DIAG BOUNDARY D3: full disconnect + NEW connector/client"
    )
    await _safe_full_disconnect(connector)
    fresh_connector, fresh_client = _new_fresh_client_from(connector)
    try:
        await _orig_client_connect_ble_only(fresh_client, device_id)
        if await _probe_filter(fresh_client.base_client, "D3"):
            _summary("RECOVERED D3 — bridge/transport object state implicated.")
            return "D3"
    except Exception as exc:
        _diag_logger.warning("DIAG BOUNDARY D3: fresh-client test failed: %s", exc)
    finally:
        await _safe_full_disconnect(fresh_connector)

    if not getattr(connector, "esphome_host", None):
        _summary(
            "D1-D3 failed and no ESPHome proxy configured — persistent WC-side state strongly suspected."
        )
        return "NO_ESPHOME"

    _diag_logger.info(
        "DIAG BOUNDARY D4: restart ESP32 proxy; WC remains powered"
    )
    try:
        await connector.restart_esp32_async()
    except Exception as exc:
        _diag_logger.warning("DIAG BOUNDARY D4: restart command failed: %s", exc)
        _summary(
            "D1-D3 failed; D4 restart could not be executed. WC-vs-proxy distinction remains open."
        )
        return "D4_RESTART_FAILED"

    _diag_logger.info("DIAG BOUNDARY D4: waiting 15 s for ESP32 boot")
    await _asyncio.sleep(15.0)
    fresh2_connector, fresh2_client = _new_fresh_client_from(connector)
    try:
        await _orig_client_connect_ble_only(fresh2_client, device_id)
        if await _probe_filter(fresh2_client.base_client, "D4"):
            _summary("RECOVERED D4 — ESPHome proxy/transport state implicated.")
            return "D4"
    except Exception as exc:
        _diag_logger.warning(
            "DIAG BOUNDARY D4: post-restart connect/test failed: %s", exc
        )
    finally:
        await _safe_full_disconnect(fresh2_connector)

    _summary(
        "D1-D4 all failed — persistent state inside the WC is strongly indicated."
    )
    return "WC"


async def _diag_get_filter_status(self):
    result = await _orig_get_filter_status(self)
    if not getattr(self, "_diag_boundary_initial_filter_seen", False):
        self._diag_boundary_initial_filter_seen = True
        self._diag_boundary_initial_success_at = _time.monotonic()
        _diag_logger.info(
            "DIAG BOUNDARY #1: normal startup 0x59 SUCCESS at global RPC #%03d — suite armed",
            _rpc_snapshot(),
        )
    return result


async def _diag_client_connect_ble_only(self, device_id: str):
    result = await _orig_client_connect_ble_only(self, device_id)
    self.base_client._diag_boundary_owner_client = self
    self.base_client._diag_boundary_device_id = device_id
    return result


async def _fail(connector, owner_client, device_id, reason):
    await _recovery_suite(connector, owner_client, device_id, reason)
    return False


async def _spl_then_filter(
    base_client,
    connector,
    owner_client,
    device_id,
    params,
    spl_label,
    filter_label,
    fail_reason,
):
    if not await _probe_spl(base_client, params, spl_label):
        await _fail(
            connector, owner_client, device_id,
            f"{spl_label} GetSPL itself timed out",
        )
        return False

    if not await _probe_filter(base_client, filter_label):
        await _fail(connector, owner_client, device_id, fail_reason)
        return False

    return True


async def _diag_get_common_settings(self):
    result = await _orig_get_common_settings(self)

    armed_at = getattr(self, "_diag_boundary_initial_success_at", None)
    already_started = getattr(self, "_diag_boundary_started", False)
    if (
        armed_at is None
        or already_started
        or (_time.monotonic() - armed_at) >= 60.0
    ):
        return result

    self._diag_boundary_started = True

    owner_client = getattr(self, "_diag_boundary_owner_client", None)
    device_id = getattr(self, "_diag_boundary_device_id", None)
    connector = self.bluetooth_le_connector

    if owner_client is None or not device_id:
        _summary("ABORTED — owner client/device id could not be resolved.")
        return result

    _diag_logger.info(
        "DIAG BOUNDARY: starting F/Q suite in SAME BLE session at global RPC count=%d",
        _rpc_snapshot(),
    )

    # F0 — verify the normal startup left 0x59 healthy.
    if not await _probe_filter(self, "F0"):
        await _fail(
            connector, owner_client, device_id,
            "F0 control failed before boundary tests",
        )
        return result

    # F1 — first meaningful byte in continuation zone, but only a safe duplicate.
    if not await _spl_then_filter(
        self, connector, owner_client, device_id,
        _SPL_F1_9_SAFE, "F1S", "F1F",
        "F1F 0x59 failed after 9-param SPL with safe duplicate; crossing the 8-param request boundary is sufficient",
    ):
        return result
    _summary(
        "F1 PASS — 9 params with a safe duplicate do not kill 0x59; merely placing meaningful data beyond the 8-param boundary is not sufficient."
    )

    # F2 — exact 10-record size, no special params 12/13.
    if not await _spl_then_filter(
        self, connector, owner_client, device_id,
        _SPL_F2_10_SAFE, "F2S", "F2F",
        "F2F 0x59 failed after 10-param SPL with only safe duplicates; 10-param request/response size alone is sufficient",
    ):
        return result
    _summary(
        "F2 PASS — a 10-param SPL with safe duplicates does not kill 0x59; request/response record count alone is not sufficient."
    )

    # F3 — one special parameter only, still 10 total parameters.
    if not await _spl_then_filter(
        self, connector, owner_client, device_id,
        _SPL_F3_10_PARAM12, "F3S", "F3F",
        "F3F 0x59 failed with param 12 present but param 13 absent; param 12 alone is sufficient",
    ):
        return result
    _summary(
        "F3 PASS — param 12 alone in a 10-param SPL does not kill 0x59."
    )

    # F4 — the other special parameter only.
    if not await _spl_then_filter(
        self, connector, owner_client, device_id,
        _SPL_F4_10_PARAM13, "F4S", "F4F",
        "F4F 0x59 failed with param 13 present but param 12 absent; param 13 alone is sufficient",
    ):
        return result
    _summary(
        "F4 PASS — param 13 alone in a 10-param SPL does not kill 0x59. If the full pair fails, the 12+13 combination/interdependence is implicated."
    )

    # Q1 — critical pair, but DO NOT immediately probe. This closes the logical
    # hole in the previous tests: a too-early failed 0x59 might itself poison state.
    if not await _probe_spl(self, _SPL_FULL_IPHONE, "Q1S"):
        await _fail(
            connector, owner_client, device_id,
            "Q1S full 12+13 SPL itself timed out",
        )
        return result

    _diag_logger.info(
        "DIAG BOUNDARY Q1: full 12+13 SPL completed; now 10.0 s ABSOLUTE QUIET — no AquaClean RPC"
    )
    quiet_rpc = _rpc_snapshot()
    await _asyncio.sleep(10.0)
    if _rpc_snapshot() != quiet_rpc:
        _diag_logger.warning(
            "DIAG BOUNDARY Q1: QUIET WINDOW VIOLATED — RPC count changed %d -> %d",
            quiet_rpc, _rpc_snapshot()
        )

    if not await _probe_filter(self, "Q1F"):
        await _fail(
            connector, owner_client, device_id,
            "Q1F 0x59 failed even though the first post-SPL AquaClean RPC was delayed 10 s; full 12+13 SPL itself leaves persistent bad state",
        )
        return result

    _summary(
        "Q1 PASS — after full 12+13 SPL, 0x59 works if the FIRST following AquaClean RPC is delayed 10 s. The SPL alone does not permanently poison 0x59."
    )

    # Q2 — determine whether any immediate next RPC is rejected, and whether a
    # successful intervening RPC can close/reset the state before 0x59.
    if not await _probe_spl(self, _SPL_FULL_IPHONE, "Q2S"):
        await _fail(
            connector, owner_client, device_id,
            "Q2S repeated full 12+13 SPL itself timed out",
        )
        return result

    _diag_logger.info(
        "DIAG BOUNDARY Q2I: IMMEDIATELY sending GetDeviceIdentification after full SPL"
    )
    try:
        await _orig_get_identification(self, 0)
        _diag_logger.info(
            "DIAG BOUNDARY Q2I: SUCCESS — immediate non-0x59 RPC responded (RPC now=%d)",
            _rpc_snapshot()
        )
    except _BLEPeripheralTimeoutError:
        await _fail(
            connector, owner_client, device_id,
            "Q2I immediate GetDeviceIdentification timed out after full SPL; the post-SPL problem is not specific to 0x59",
        )
        return result

    if not await _probe_filter(self, "Q2F"):
        await _fail(
            connector, owner_client, device_id,
            "Q2F identification succeeded immediately after full SPL but subsequent 0x59 failed; intervening RPC does not reset the 0x59-specific state",
        )
        return result

    _summary(
        "Q2 PASS — an immediate GetDeviceIdentification plus subsequent 0x59 both work after full SPL. A successful intervening RPC and/or its short processing delay avoids the failure."
    )

    # Q3 — pure timing test, no intervening request.
    if not await _probe_spl(self, _SPL_FULL_IPHONE, "Q3S"):
        await _fail(
            connector, owner_client, device_id,
            "Q3S repeated full 12+13 SPL itself timed out",
        )
        return result

    _diag_logger.info(
        "DIAG BOUNDARY Q3: waiting 1.0 s with NO AquaClean RPC after full SPL"
    )
    quiet_rpc = _rpc_snapshot()
    await _asyncio.sleep(1.0)
    if _rpc_snapshot() != quiet_rpc:
        _diag_logger.warning(
            "DIAG BOUNDARY Q3: QUIET WINDOW VIOLATED — RPC count changed %d -> %d",
            quiet_rpc, _rpc_snapshot()
        )

    if not await _probe_filter(self, "Q3F"):
        await _fail(
            connector, owner_client, device_id,
            "Q3F 0x59 failed after 1 s quiet but Q1 survived 10 s quiet; required post-SPL quiet time is >1 s and <=10 s (subject to repeatability)",
        )
        return result

    _summary(
        "Q3 PASS — 1 s quiet is sufficient after full 12+13 SPL."
    )

    # Q4 — known destructive sequence, deliberately last.
    if not await _probe_spl(self, _SPL_FULL_IPHONE, "Q4S"):
        await _fail(
            connector, owner_client, device_id,
            "Q4S repeated full 12+13 SPL itself timed out",
        )
        return result

    _diag_logger.info(
        "DIAG BOUNDARY Q4: NO intentional delay — 0x59 follows immediately"
    )
    if not await _probe_filter(self, "Q4F"):
        await _fail(
            connector, owner_client, device_id,
            "Q4F direct 0x59 failed after full SPL while delayed/intervening variants survived; immediate post-SPL sequencing/timing is strongly implicated",
        )
        return result

    _summary(
        "F0-F4 + Q1-Q4 ALL PASS — even the direct full-SPL -> 0x59 sequence survived. Earlier failure is non-deterministic or depends on an unmeasured timing/state variable."
    )

    return result


_AquaCleanBaseClient.send_request = _diag_send_request
_AquaCleanBaseClient.get_filter_status_async = _diag_get_filter_status
_AquaCleanBaseClient.get_stored_common_settings_async = _diag_get_common_settings
_AquaCleanClient.connect_ble_only = _diag_client_connect_ble_only
