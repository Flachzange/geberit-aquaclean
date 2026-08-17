import importlib.util
import logging
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).parents[1]
    / "aquaclean_console_app"
    / "bluetooth_le"
    / "LE"
    / "GattDiag.py"
)


def _load_diag_module():
    spec = importlib.util.spec_from_file_location("aquaclean_gatt_diag_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Descriptor:
    def __init__(self, uuid, handle):
        self.uuid = uuid
        self.handle = handle


class _Characteristic:
    def __init__(self, uuid, handle, cccd):
        self.uuid = uuid
        self.handle = handle
        self.descriptors = [
            _Descriptor("00002902-0000-1000-8000-00805f9b34fb", cccd)
        ]


class _Service:
    uuid = "3334429d-90f3-4c41-a02d-5cb3a03e0000"

    def __init__(self):
        self.characteristics = [
            _Characteristic("3334429d-90f3-4c41-a02d-5cb3a53e0000", 0x0F, 0x10),
            _Characteristic("3334429d-90f3-4c41-a02d-5cb3a63e0000", 0x13, 0x14),
            _Characteristic("3334429d-90f3-4c41-a02d-5cb3a73e0000", 0x17, 0x18),
            _Characteristic("3334429d-90f3-4c41-a02d-5cb3a83e0000", 0x1B, 0x1C),
        ]


class _ServicesResponse:
    def __init__(self):
        self.services = [_Service()]


class _FakeAPIClient:
    async def bluetooth_gatt_get_services(self, address):
        return _ServicesResponse()

    async def bluetooth_gatt_start_notify(self, address, handle, callback):
        return (lambda: None, lambda: None)

    async def bluetooth_gatt_write_descriptor(self, address, handle, data):
        return None


class _FailingDescriptorAPIClient:
    async def bluetooth_gatt_get_services(self, address):
        return _ServicesResponse()

    async def bluetooth_gatt_start_notify(self, address, handle, callback):
        return (lambda: None, lambda: None)

    async def bluetooth_gatt_write_descriptor(self, address, handle, data):
        raise RuntimeError("simulated status 133")


@pytest.mark.asyncio
async def test_diag_maps_a6_cccd_and_logs_combined_timing(caplog, monkeypatch):
    diag = _load_diag_module()

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(diag.asyncio, "sleep", _no_sleep)
    assert diag.install(_FakeAPIClient) is True
    assert diag.install(_FakeAPIClient) is False

    api = _FakeAPIClient()
    address = int("38AB412A0D67", 16)

    with caplog.at_level(logging.INFO):
        await api.bluetooth_gatt_get_services(address)
        await api.bluetooth_gatt_start_notify(address, 0x13, lambda *_: None)
        await api.bluetooth_gatt_write_descriptor(address, 0x14, b"\x01\x00")

    text = caplog.text
    assert "READ_1(A6):char=0x0013/cccd=0x0014" in text
    assert "stage=notify_setup OK" in text
    assert "role=READ_1(A6)" in text
    assert "char=0x0013" in text
    assert "cccd=0x0014" in text
    assert "register_ms=" in text
    assert "gap_after_register_ms=" in text
    assert "cccd_ms=" in text


@pytest.mark.asyncio
async def test_diag_identifies_cccd_failure_stage_and_reraises(caplog):
    diag = _load_diag_module()
    assert diag.install(_FailingDescriptorAPIClient) is True

    api = _FailingDescriptorAPIClient()
    address = int("38AB412A0D67", 16)

    with caplog.at_level(logging.INFO):
        await api.bluetooth_gatt_get_services(address)
        await api.bluetooth_gatt_start_notify(address, 0x13, lambda *_: None)
        with pytest.raises(RuntimeError, match="simulated status 133"):
            await api.bluetooth_gatt_write_descriptor(address, 0x14, b"\x01\x00")

    text = caplog.text
    assert "stage=cccd_write ERROR" in text
    assert "role=READ_1(A6)" in text
    assert "char=0x0013" in text
    assert "cccd=0x0014" in text
    assert "register_ms=" in text
    assert "cccd_elapsed_ms=" in text
    assert "RuntimeError: simulated status 133" in text


@pytest.mark.asyncio
async def test_diag_settles_after_a5_to_a7_but_not_a8(caplog, monkeypatch):
    diag = _load_diag_module()

    sleeps = []

    async def _record_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(diag.asyncio, "sleep", _record_sleep)

    class _SettlingAPIClient:
        async def bluetooth_gatt_get_services(self, address):
            return _ServicesResponse()

        async def bluetooth_gatt_start_notify(self, address, handle, callback):
            return (lambda: None, lambda: None)

        async def bluetooth_gatt_write_descriptor(self, address, handle, data):
            return None

    assert diag.install(_SettlingAPIClient) is True
    api = _SettlingAPIClient()
    address = int("38AB412A0D67", 16)

    with caplog.at_level(logging.INFO):
        await api.bluetooth_gatt_get_services(address)

        # A5: settling pause is part of experiment #2.
        await api.bluetooth_gatt_start_notify(address, 0x0F, lambda *_: None)
        await api.bluetooth_gatt_write_descriptor(address, 0x10, b"\x01\x00")

        # A8: final channel, therefore no trailing pause.
        await api.bluetooth_gatt_start_notify(address, 0x1B, lambda *_: None)
        await api.bluetooth_gatt_write_descriptor(address, 0x1C, b"\x01\x00")

    assert sleeps == [pytest.approx(0.250)]
    text = caplog.text
    assert "stage=inter_channel_settle BEGIN" in text
    assert "role=READ_0(A5)" in text
    assert "settle_ms=250.0" in text
    assert "stage=inter_channel_settle OK" in text
