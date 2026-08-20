"""Temporary ESPHome GATT notification diagnostics.

This module instruments the three aioesphomeapi operations involved in the
AquaClean notification setup without changing their order, parameters, retry
behaviour, or timing intentionally:

* bluetooth_gatt_get_services()       -> records characteristic/CCCD handles
* bluetooth_gatt_start_notify()       -> times notification registration
* bluetooth_gatt_write_descriptor()  -> times the CCCD write

It exists for the diag/esphome-cccd-status133 branch and should be removed once
the intermittent ESP_GATT_ERROR (133) has been isolated.
"""

from __future__ import annotations

import functools
import itertools
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_SERVICE_UUID = "3334429d-90f3-4c41-a02d-5cb3a03e0000"
_CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"
_SESSION_SEQ = itertools.count(1)
_INSTALLED_ATTR = "_aquaclean_gatt_diag_installed"

_READ_LABELS = {
    "3334429d-90f3-4c41-a02d-5cb3a53e0000": "READ_0(A5)",
    "3334429d-90f3-4c41-a02d-5cb3a63e0000": "READ_1(A6)",
    "3334429d-90f3-4c41-a02d-5cb3a73e0000": "READ_2(A7)",
    "3334429d-90f3-4c41-a02d-5cb3a83e0000": "READ_3(A8)",
}


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def _fmt_handle(value: Any) -> str:
    try:
        return f"0x{int(value):04x}"
    except (TypeError, ValueError):
        return str(value)


def _arg(args: tuple, kwargs: dict, pos: int, *names: str, default=None):
    if len(args) > pos:
        return args[pos]
    for name in names:
        if name in kwargs:
            return kwargs[name]
    return default


def _session(api: Any) -> int:
    return int(getattr(api, "_aquaclean_gatt_diag_session", 0) or 0)


def _char_meta(api: Any, handle: Any) -> tuple[str, str]:
    try:
        key = int(handle)
    except (TypeError, ValueError):
        return ("unknown", "unknown")
    meta = getattr(api, "_aquaclean_gatt_diag_chars", {}).get(key)
    if meta is None:
        return ("unknown", "unknown")
    return meta


def _cccd_meta(api: Any, cccd_handle: Any) -> tuple[Any, str, str]:
    try:
        key = int(cccd_handle)
    except (TypeError, ValueError):
        return (None, "unknown", "unknown")
    meta = getattr(api, "_aquaclean_gatt_diag_cccd", {}).get(key)
    if meta is None:
        return (None, "unknown", "unknown")
    return meta


def _extract_geberit_map(response: Any) -> tuple[dict[int, tuple[str, str]], dict[int, tuple[int, str, str]], str]:
    chars: dict[int, tuple[str, str]] = {}
    cccds: dict[int, tuple[int, str, str]] = {}
    plan: list[str] = []

    for service in getattr(response, "services", ()) or ():
        service_uuid = str(getattr(service, "uuid", "")).lower()
        if service_uuid != _SERVICE_UUID:
            continue
        for char in getattr(service, "characteristics", ()) or ():
            uuid = str(getattr(char, "uuid", "")).lower()
            handle = int(getattr(char, "handle"))
            role = _READ_LABELS.get(uuid, uuid)
            chars[handle] = (uuid, role)

            cccd_handle = None
            for desc in getattr(char, "descriptors", ()) or ():
                if str(getattr(desc, "uuid", "")).lower() == _CCCD_UUID:
                    cccd_handle = int(getattr(desc, "handle"))
                    cccds[cccd_handle] = (handle, uuid, role)
                    break

            if uuid in _READ_LABELS:
                plan.append(
                    f"{role}:char={_fmt_handle(handle)}/cccd="
                    f"{_fmt_handle(cccd_handle) if cccd_handle is not None else 'none'}"
                )

    return chars, cccds, ", ".join(plan) if plan else "no Geberit READ characteristics found"


def install(api_client_cls=None) -> bool:
    """Install the diagnostic wrappers once; return True only on first install.

    ``api_client_cls`` is injectable for unit tests. Production calls this with
    no argument and therefore patches ``aioesphomeapi.APIClient``.
    """
    if api_client_cls is None:
        from aioesphomeapi import APIClient

        api_client_cls = APIClient

    if getattr(api_client_cls, _INSTALLED_ATTR, False):
        return False

    original_get_services = api_client_cls.bluetooth_gatt_get_services
    original_start_notify = api_client_cls.bluetooth_gatt_start_notify
    original_write_descriptor = api_client_cls.bluetooth_gatt_write_descriptor

    @functools.wraps(original_get_services)
    async def get_services_diag(self, *args, **kwargs):
        address = _arg(args, kwargs, 0, "address")
        started = time.perf_counter()
        try:
            response = await original_get_services(self, *args, **kwargs)
        except Exception as exc:
            logger.error(
                "[GATT-DIAG] session=%s stage=service_discovery ERROR address=%s elapsed_ms=%.1f error=%s: %s",
                _session(self),
                address,
                _elapsed_ms(started),
                type(exc).__name__,
                exc,
            )
            raise

        session = next(_SESSION_SEQ)
        chars, cccds, plan = _extract_geberit_map(response)
        setattr(self, "_aquaclean_gatt_diag_session", session)
        setattr(self, "_aquaclean_gatt_diag_chars", chars)
        setattr(self, "_aquaclean_gatt_diag_cccd", cccds)
        setattr(self, "_aquaclean_gatt_diag_notify_ms", {})
        setattr(self, "_aquaclean_gatt_diag_notify_end", {})

        logger.info(
            "[GATT-DIAG] session=%s stage=service_discovery OK address=%s elapsed_ms=%.1f plan=%s",
            session,
            address,
            _elapsed_ms(started),
            plan,
        )
        return response

    @functools.wraps(original_start_notify)
    async def start_notify_diag(self, *args, **kwargs):
        address = _arg(args, kwargs, 0, "address")
        handle = _arg(args, kwargs, 1, "handle")
        uuid, role = _char_meta(self, handle)
        started = time.perf_counter()
        logger.debug(
            "[GATT-DIAG] session=%s stage=notify_register BEGIN address=%s role=%s char=%s uuid=%s",
            _session(self),
            address,
            role,
            _fmt_handle(handle),
            uuid,
        )
        try:
            result = await original_start_notify(self, *args, **kwargs)
        except Exception as exc:
            logger.error(
                "[GATT-DIAG] session=%s stage=notify_register ERROR address=%s role=%s char=%s uuid=%s elapsed_ms=%.1f error=%s: %s",
                _session(self),
                address,
                role,
                _fmt_handle(handle),
                uuid,
                _elapsed_ms(started),
                type(exc).__name__,
                exc,
            )
            raise

        elapsed = _elapsed_ms(started)
        try:
            key = int(handle)
            getattr(self, "_aquaclean_gatt_diag_notify_ms", {})[key] = elapsed
            getattr(self, "_aquaclean_gatt_diag_notify_end", {})[key] = time.perf_counter()
        except (TypeError, ValueError):
            pass
        logger.debug(
            "[GATT-DIAG] session=%s stage=notify_register OK address=%s role=%s char=%s uuid=%s elapsed_ms=%.1f",
            _session(self),
            address,
            role,
            _fmt_handle(handle),
            uuid,
            elapsed,
        )
        return result

    @functools.wraps(original_write_descriptor)
    async def write_descriptor_diag(self, *args, **kwargs):
        address = _arg(args, kwargs, 0, "address")
        cccd_handle = _arg(args, kwargs, 1, "handle", "descriptor_handle")
        char_handle, uuid, role = _cccd_meta(self, cccd_handle)
        notify_ms = None
        notify_end = None
        if char_handle is not None:
            notify_ms = getattr(self, "_aquaclean_gatt_diag_notify_ms", {}).get(char_handle)
            notify_end = getattr(self, "_aquaclean_gatt_diag_notify_end", {}).get(char_handle)
        gap_ms = (time.perf_counter() - notify_end) * 1000.0 if notify_end is not None else None

        started = time.perf_counter()
        logger.debug(
            "[GATT-DIAG] session=%s stage=cccd_write BEGIN address=%s role=%s char=%s cccd=%s uuid=%s gap_after_register_ms=%s",
            _session(self),
            address,
            role,
            _fmt_handle(char_handle),
            _fmt_handle(cccd_handle),
            uuid,
            f"{gap_ms:.1f}" if gap_ms is not None else "n/a",
        )
        try:
            result = await original_write_descriptor(self, *args, **kwargs)
        except Exception as exc:
            logger.error(
                "[GATT-DIAG] session=%s stage=cccd_write ERROR address=%s role=%s char=%s cccd=%s uuid=%s register_ms=%s gap_after_register_ms=%s cccd_elapsed_ms=%.1f error=%s: %s",
                _session(self),
                address,
                role,
                _fmt_handle(char_handle),
                _fmt_handle(cccd_handle),
                uuid,
                f"{notify_ms:.1f}" if notify_ms is not None else "n/a",
                f"{gap_ms:.1f}" if gap_ms is not None else "n/a",
                _elapsed_ms(started),
                type(exc).__name__,
                exc,
            )
            raise

        cccd_ms = _elapsed_ms(started)
        total_ms = (notify_ms or 0.0) + (gap_ms or 0.0) + cccd_ms
        logger.info(
            "[GATT-DIAG] session=%s stage=notify_setup OK address=%s role=%s char=%s cccd=%s uuid=%s register_ms=%s gap_after_register_ms=%s cccd_ms=%.1f total_ms=%.1f",
            _session(self),
            address,
            role,
            _fmt_handle(char_handle),
            _fmt_handle(cccd_handle),
            uuid,
            f"{notify_ms:.1f}" if notify_ms is not None else "n/a",
            f"{gap_ms:.1f}" if gap_ms is not None else "n/a",
            cccd_ms,
            total_ms,
        )
        return result

    api_client_cls.bluetooth_gatt_get_services = get_services_diag
    api_client_cls.bluetooth_gatt_start_notify = start_notify_diag
    api_client_cls.bluetooth_gatt_write_descriptor = write_descriptor_diag
    setattr(api_client_cls, _INSTALLED_ATTR, True)

    logger.info("[GATT-DIAG] aioesphomeapi GATT notification diagnostics enabled")
    return True
