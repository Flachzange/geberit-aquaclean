"""Temporary GetFilterStatus session-poisoning diagnostic.

This file is intentionally a test-only shim.  It instruments AquaCleanBaseClient
without changing the production bridge flow in main.py.
"""

import logging as _logging
import time as _time

from aquaclean_console_app.aquaclean_core.Clients.AquaCleanBaseClient import (
    AquaCleanBaseClient as _AquaCleanBaseClient,
    BLEPeripheralTimeoutError as _BLEPeripheralTimeoutError,
)

_diag_logger = _logging.getLogger("aquaclean_console_app.main")
_orig_get_filter_status = _AquaCleanBaseClient.get_filter_status_async
_orig_get_common_settings = _AquaCleanBaseClient.get_stored_common_settings_async


async def _diag_get_filter_status(self):
    result = await _orig_get_filter_status(self)
    # Only a successful normal call arms the second probe.  The probe itself uses
    # _orig_get_filter_status directly and therefore never re-arms this marker.
    self._diag_filterstatus_initial_success_at = _time.monotonic()
    return result


async def _diag_get_common_settings(self):
    result = await _orig_get_common_settings(self)

    initial_success_at = getattr(self, "_diag_filterstatus_initial_success_at", None)
    already_done = getattr(self, "_diag_filterstatus_second_done", False)

    # _fetch_state_and_info performs GetFilterStatus -> GetSPL -> identification ->
    # profile settings -> common settings in one session.  A short time window keeps
    # the shim from probing unrelated later common-settings requests.
    if initial_success_at is not None and not already_done and (_time.monotonic() - initial_success_at) < 30.0:
        self._diag_filterstatus_second_done = True
        _diag_logger.info("DIAG GetFilterStatus #2: testing 0x59 again before BLE disconnect")
        try:
            await _orig_get_filter_status(self)
            _diag_logger.info("DIAG GetFilterStatus #2: SUCCESS — 0x59 still works before BLE disconnect")
        except _BLEPeripheralTimeoutError:
            _diag_logger.warning("DIAG GetFilterStatus #2: TIMEOUT — 0x59 became stuck during this BLE session")

    return result


_AquaCleanBaseClient.get_filter_status_async = _diag_get_filter_status
_AquaCleanBaseClient.get_stored_common_settings_async = _diag_get_common_settings
