"""Temporary GetFilterStatus session-poisoning diagnostic.

Test-only shim. It performs:
  #1 normal GetFilterStatus during startup
  #2 GetFilterStatus at the end of the same BLE session
  disconnect_ble_only()
  immediate reconnect
  #3 GetFilterStatus in the new BLE session
  #4 GetFilterStatus immediately after the first later GetSPL call

Production bridge flow in main.py remains unchanged.
"""

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
_orig_get_filter_status = _AquaCleanBaseClient.get_filter_status_async
_orig_get_common_settings = _AquaCleanBaseClient.get_stored_common_settings_async
_orig_get_spl = _AquaCleanBaseClient.get_system_parameter_list_async
_orig_client_connect_ble_only = _AquaCleanClient.connect_ble_only
_orig_connector_disconnect_ble_only = _BluetoothLeConnector.disconnect_ble_only
_diag_clients_by_connector = {}


async def _diag_get_filter_status(self):
    result = await _orig_get_filter_status(self)
    # Only a successful normal call arms the in-session probe.
    self._diag_filterstatus_initial_success_at = _time.monotonic()
    return result


async def _diag_get_common_settings(self):
    result = await _orig_get_common_settings(self)

    initial_success_at = getattr(self, "_diag_filterstatus_initial_success_at", None)
    already_done = getattr(self, "_diag_filterstatus_second_done", False)

    if initial_success_at is not None and not already_done and (_time.monotonic() - initial_success_at) < 30.0:
        self._diag_filterstatus_second_done = True
        _diag_logger.info("DIAG GetFilterStatus #2: testing 0x59 again before BLE disconnect")
        try:
            await _orig_get_filter_status(self)
            self._diag_filterstatus_second_success = True
            _diag_logger.info("DIAG GetFilterStatus #2: SUCCESS — 0x59 still works before BLE disconnect")
        except _BLEPeripheralTimeoutError:
            self._diag_filterstatus_second_success = False
            _diag_logger.warning("DIAG GetFilterStatus #2: TIMEOUT — 0x59 became stuck during this BLE session")

    return result


async def _diag_client_connect_ble_only(self, device_id: str):
    result = await _orig_client_connect_ble_only(self, device_id)
    connector = self.base_client.bluetooth_le_connector
    _diag_clients_by_connector[id(connector)] = (self, device_id)
    return result


async def _diag_disconnect_ble_only(self):
    entry = _diag_clients_by_connector.get(id(self))
    probe = False
    client = None
    device_id = None
    base_client = None

    if entry is not None:
        client, device_id = entry
        base_client = client.base_client
        probe = (
            getattr(base_client, "_diag_filterstatus_second_success", False)
            and not getattr(base_client, "_diag_filterstatus_post_disconnect_done", False)
        )
        if probe:
            # Arm before disconnect/reconnect so the cleanup disconnect below cannot recurse.
            base_client._diag_filterstatus_post_disconnect_done = True

    # This is the production disconnect we are testing.
    await _orig_connector_disconnect_ble_only(self)

    if not probe:
        return

    _diag_logger.info("DIAG GetFilterStatus #3: BLE disconnected; reconnecting immediately for isolated 0x59 test")
    try:
        # Reuse the same client/connector exactly as persistent ESPHome API mode does.
        await _orig_client_connect_ble_only(client, device_id)
        _diag_logger.info("DIAG GetFilterStatus #3: BLE reconnect successful; testing 0x59")
        try:
            await _orig_get_filter_status(base_client)
            base_client._diag_filterstatus_post_disconnect_success = True
            _diag_logger.info("DIAG GetFilterStatus #3: SUCCESS — 0x59 survives disconnect/reconnect")
        except _BLEPeripheralTimeoutError:
            base_client._diag_filterstatus_post_disconnect_success = False
            _diag_logger.warning("DIAG GetFilterStatus #3: TIMEOUT — 0x59 becomes stuck immediately after disconnect/reconnect")
    except Exception as exc:
        _diag_logger.warning("DIAG GetFilterStatus #3: RECONNECT FAILED — %s", exc)
    finally:
        try:
            await _orig_connector_disconnect_ble_only(self)
        except Exception:
            pass


async def _diag_get_spl(self, *args, **kwargs):
    result = await _orig_get_spl(self, *args, **kwargs)

    # After #3 proved that a bare disconnect/reconnect is harmless, the first
    # later SPL call is the next isolated candidate. Probe 0x59 immediately after
    # that SPL response, while the same BLE session is still open.
    ready = getattr(self, "_diag_filterstatus_post_disconnect_success", False)
    already_done = getattr(self, "_diag_filterstatus_fourth_done", False)
    if ready and not already_done:
        self._diag_filterstatus_fourth_done = True
        _diag_logger.info(
            "DIAG GetFilterStatus #4: first SPL after successful #3 completed; "
            "testing 0x59 before BLE disconnect"
        )
        try:
            await _orig_get_filter_status(self)
            self._diag_filterstatus_fourth_success = True
            _diag_logger.info(
                "DIAG GetFilterStatus #4: SUCCESS — 0x59 still works immediately after later SPL"
            )
        except _BLEPeripheralTimeoutError:
            self._diag_filterstatus_fourth_success = False
            _diag_logger.warning(
                "DIAG GetFilterStatus #4: TIMEOUT — later SPL makes 0x59 stuck before BLE disconnect"
            )

    return result


_AquaCleanBaseClient.get_filter_status_async = _diag_get_filter_status
_AquaCleanBaseClient.get_stored_common_settings_async = _diag_get_common_settings
_AquaCleanBaseClient.get_system_parameter_list_async = _diag_get_spl
_AquaCleanClient.connect_ble_only = _diag_client_connect_ble_only
_BluetoothLeConnector.disconnect_ble_only = _diag_disconnect_ble_only
