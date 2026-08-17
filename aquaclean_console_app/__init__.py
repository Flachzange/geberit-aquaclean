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

# === DIAG H-SEQUENCE OVERRIDE v1 ===
#
# This block deliberately overrides ONLY the diagnostic common-settings hook.
# Existing RPC numbering, initial GetFilterStatus arming, and D1-D4 recovery
# remain unchanged above.
#
# H sequence:
#   H0   baseline 0x59
#   H1   fixed-13, 8 params with duplicate 4 -> immediate 0x59
#   H2A  fixed-13, 9 params tail=0 -> 10 s absolute quiet -> 0x59
#   H2B  fixed-13, 9 params tail=0 -> immediate 0x59
#   H3A  COMPACT, 9 params tail=4 -> 10 s absolute quiet -> 0x59
#   H3B  COMPACT, 9 params tail=4 -> immediate 0x59
#   H4   fixed-13, 9 params tail=4 -> 10 s absolute quiet -> 0x59
#
# Stop at the first failed GetSPL/GetFilterStatus and run the already-existing
# D1-D4 recovery cascade. H4 is deliberately last because fixed-13 + tail=4 is
# the known destructive sequence from the previous F1 run.

from aquaclean_console_app.aquaclean_core.Api.CallClasses.GetSystemParameterList import (
    GetSystemParameterList as _H_GetSystemParameterList,
)

_H_SPL_8_DUP4 = [0, 1, 2, 3, 4, 5, 6, 4]
_H_SPL_9_TAIL0 = [0, 1, 2, 3, 4, 5, 6, 7, 0]
_H_SPL_9_TAIL4 = [0, 1, 2, 3, 4, 5, 6, 7, 4]


class _H_CompactGetSystemParameterList(_H_GetSystemParameterList):
    """Diagnostic-only GetSPL with variable argument length: count + IDs."""

    def get_payload(self):
        arg_count = min(len(self.parameter_list), 12)
        data = bytearray(1 + arg_count)
        data[0] = arg_count
        for i in range(arg_count):
            data[i + 1] = self.parameter_list[i]
        return data


def _h_wire_log(base_client, api_call, label: str, mode: str):
    """Log the exact bytes the current sender will derive, without sending."""

    payload = api_call.get_payload()
    rpc_body = base_client.build_payload(api_call)
    message = base_client.message_service.build_message(rpc_body)
    serialized_message = message.serialize()

    frame = base_client.frame_factory.BuildSingleFrame(serialized_message)
    frame.SubFrameCountOrIndex = 1
    write_0 = frame.serialize()

    # Keep this IDENTICAL to AquaCleanBaseClient.send_request().
    write_1 = bytes([0x12]) + serialized_message[19:38]

    crc_message_logical = serialized_message[: 6 + len(rpc_body)]

    _diag_logger.info(
        "DIAG H WIRE %s: mode=%s params=%s payload_len=%d rpc_body_len=%d",
        label, mode, list(api_call.parameter_list), len(payload), len(rpc_body),
    )
    _diag_logger.info(
        "DIAG H WIRE %s: PAYLOAD=%s", label, payload.hex()
    )
    _diag_logger.info(
        "DIAG H WIRE %s: RPC_BODY=%s", label, rpc_body.hex()
    )
    _diag_logger.info(
        "DIAG H WIRE %s: CRC_MESSAGE=%s", label, crc_message_logical.hex()
    )
    _diag_logger.info(
        "DIAG H WIRE %s: WRITE_0=%s", label, bytes(write_0).hex()
    )
    _diag_logger.info(
        "DIAG H WIRE %s: WRITE_1=%s", label, write_1.hex()
    )


async def _h_probe_spl(base_client, params, label: str, compact: bool = False) -> bool:
    mode = "compact" if compact else "fixed13"
    api_call_cls = (
        _H_CompactGetSystemParameterList
        if compact
        else _H_GetSystemParameterList
    )
    api_call = api_call_cls(list(params))

    _diag_logger.info(
        "DIAG H %s: GetSPL mode=%s params=%s (RPC before=%d)",
        label, mode, params, _rpc_snapshot(),
    )

    try:
        _h_wire_log(base_client, api_call, label, mode)

        response = await base_client.send_request(
            api_call, send_as_first_cons=True
        )
        result = response.result(base_client.message_context.result_bytes)

        _diag_logger.info(
            "DIAG H %s: SUCCESS — GetSPL completed mode=%s result_count=%s "
            "(RPC now=%d)",
            label,
            mode,
            getattr(result, "a", "?"),
            _rpc_snapshot(),
        )
        return True

    except _BLEPeripheralTimeoutError:
        _diag_logger.warning(
            "DIAG H %s: TIMEOUT — GetSPL itself did not complete mode=%s "
            "(RPC now=%d)",
            label, mode, _rpc_snapshot(),
        )
        return False

    except Exception as exc:
        _diag_logger.exception(
            "DIAG H %s: ERROR — GetSPL mode=%s failed: %s: %s",
            label, mode, type(exc).__name__, exc,
        )
        return False


async def _h_quiet(seconds: float, label: str) -> bool:
    """Require a truly RPC-free quiet window."""

    before = _rpc_snapshot()
    _diag_logger.info(
        "DIAG H %s: %.1f s ABSOLUTE QUIET — no AquaClean RPC "
        "(RPC count=%d)",
        label, seconds, before,
    )

    await _asyncio.sleep(seconds)

    after = _rpc_snapshot()
    if after != before:
        _diag_logger.warning(
            "DIAG H %s: QUIET WINDOW VIOLATED — RPC count changed %d -> %d",
            label, before, after,
        )
        _summary(
            "%s INVALID — intended quiet window was violated by another "
            "AquaClean RPC (%d -> %d). Suite stopped without interpreting "
            "the following state.",
            label, before, after,
        )
        return False

    _diag_logger.info(
        "DIAG H %s: quiet window clean — RPC count stayed at %d",
        label, after,
    )
    return True


async def _h_fail(connector, owner_client, device_id, reason: str):
    await _fail(connector, owner_client, device_id, reason)
    return False


async def _h_spl_then_filter(
    base_client,
    connector,
    owner_client,
    device_id,
    params,
    spl_label: str,
    filter_label: str,
    fail_reason: str,
    *,
    compact: bool = False,
    quiet_seconds: float = 0.0,
) -> bool:
    if not await _h_probe_spl(
        base_client, params, spl_label, compact=compact
    ):
        await _h_fail(
            connector,
            owner_client,
            device_id,
            f"{spl_label} GetSPL itself failed/timed out",
        )
        return False

    if quiet_seconds > 0.0:
        if not await _h_quiet(quiet_seconds, f"{spl_label}-QUIET"):
            return False
    else:
        _diag_logger.info(
            "DIAG H %s: NO intentional delay — 0x59 follows immediately",
            filter_label,
        )

    if not await _probe_filter(base_client, filter_label):
        await _h_fail(
            connector, owner_client, device_id, fail_reason
        )
        return False

    return True


async def _diag_h_get_common_settings(self):
    # Preserve the normal startup call exactly as before. The H suite begins
    # only after the original common-settings request has completed.
    result = await _orig_get_common_settings(self)

    armed_at = getattr(self, "_diag_boundary_initial_success_at", None)
    already_started = getattr(self, "_diag_h_started", False)

    if (
        armed_at is None
        or already_started
        or (_time.monotonic() - armed_at) >= 60.0
    ):
        return result

    self._diag_h_started = True

    owner_client = getattr(self, "_diag_boundary_owner_client", None)
    device_id = getattr(self, "_diag_boundary_device_id", None)
    connector = self.bluetooth_le_connector

    if owner_client is None or not device_id:
        _summary(
            "H SUITE ABORTED — owner client/device id could not be resolved."
        )
        return result

    _diag_logger.info(
        "DIAG H: starting combined 8→9 / tail-byte / compact-length / "
        "quiet-time suite in SAME BLE session at global RPC count=%d",
        _rpc_snapshot(),
    )

    # H0 — baseline.
    if not await _probe_filter(self, "H0"):
        await _h_fail(
            connector,
            owner_client,
            device_id,
            "H0 baseline failed before H tests",
        )
        return result

    # H1 — eliminate duplicate-parameter as the explanation while staying
    # below the 8->9 boundary.
    if not await _h_spl_then_filter(
        self,
        connector,
        owner_client,
        device_id,
        _H_SPL_8_DUP4,
        "H1S",
        "H1F",
        "H1F 0x59 failed after fixed-13 8-param SPL with duplicate 4; "
        "duplicate parameters are implicated and the 8→9 theory is weakened",
    ):
        return result

    _summary(
        "H1 PASS — duplicate parameter 4 is tolerated with 8 parameters; "
        "the previous F1 failure is not explained by the duplicate alone."
    )

    # H2A — count=9 but ninth byte is 0x00, so WRITE_1 remains all-zero.
    # First post-SPL RPC is delayed by 10 s.
    if not await _h_spl_then_filter(
        self,
        connector,
        owner_client,
        device_id,
        _H_SPL_9_TAIL0,
        "H2AS",
        "H2AF",
        "H2AF 0x59 failed after fixed-13 9-param SPL with tail=0 even "
        "after 10 s quiet; count=9 / transaction shape is sufficient and "
        "a non-zero CONS payload byte is not required",
        quiet_seconds=10.0,
    ):
        return result

    _summary(
        "H2A PASS — fixed-13 count=9 with tail=0 survives when the FIRST "
        "post-SPL RPC is delayed 10 s."
    )

    # H2B — same bytes/semantics as H2A but immediate follow-up.
    if not await _h_spl_then_filter(
        self,
        connector,
        owner_client,
        device_id,
        _H_SPL_9_TAIL0,
        "H2BS",
        "H2BF",
        "H2BF 0x59 failed for fixed-13 9-param tail=0 only when immediate; "
        "post-SPL timing/quiet-time is implicated even with an all-zero CONS",
    ):
        return result

    _summary(
        "H2B PASS — fixed-13 count=9 with tail=0 also survives an immediate "
        "0x59; count=9 alone is not sufficient."
    )

    # H3A — same 9-param semantics as the known killer, but compact logical
    # payload. WRITE_1 remains byte-for-byte 12 04 00...00; only FIRST's
    # declared lengths/CRC differ.
    if not await _h_spl_then_filter(
        self,
        connector,
        owner_client,
        device_id,
        _H_SPL_9_TAIL4,
        "H3AS",
        "H3AF",
        "H3AF 0x59 failed after COMPACT 9-param tail=4 even after 10 s quiet; "
        "compact logical length alone does not prevent the bad state",
        compact=True,
        quiet_seconds=10.0,
    ):
        return result

    _summary(
        "H3A PASS — compact 9-param tail=4 survives after 10 s quiet."
    )

    # H3B — compact request, immediate follow-up.
    if not await _h_spl_then_filter(
        self,
        connector,
        owner_client,
        device_id,
        _H_SPL_9_TAIL4,
        "H3BS",
        "H3BF",
        "H3BF 0x59 failed for COMPACT 9-param tail=4 only when immediate; "
        "compact encoding helps with persistent poisoning but an immediate "
        "post-SPL timing constraint remains",
        compact=True,
    ):
        return result

    _summary(
        "H3B PASS — COMPACT 9-param tail=4 also survives immediate 0x59. "
        "The non-zero CONS byte itself is therefore not sufficient."
    )

    # H4 — fixed-13 known-killer semantics, but FIRST following RPC is delayed
    # 10 s. Deliberately last: previous F1 already proved the immediate variant
    # can wedge 0x59 persistently.
    if not await _h_spl_then_filter(
        self,
        connector,
        owner_client,
        device_id,
        _H_SPL_9_TAIL4,
        "H4S",
        "H4F",
        "H4F 0x59 failed after fixed-13 9-param tail=4 despite 10 s absolute "
        "quiet, while compact H3 survived; fixed logical payload length / "
        "declared message length / CRC framing is strongly implicated",
        quiet_seconds=10.0,
    ):
        return result

    _summary(
        "H0-H4 ALL PASS — fixed-13 9-param tail=4 survives if the FIRST "
        "post-SPL RPC is delayed 10 s, while the previous run showed the "
        "immediate variant wedges 0x59. Quiet-time/timing is therefore the "
        "leading discriminator; compact encoding also survived both variants."
    )

    return result


# Override only the old F/Q suite trigger. All previously defined diagnostic
# helpers, RPC numbering, startup arming and D1-D4 recovery remain active.
_AquaCleanBaseClient.get_stored_common_settings_async = _diag_h_get_common_settings
# === END DIAG H-SEQUENCE OVERRIDE v1 ===
