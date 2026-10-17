"""Regression tests for Mera orientation light settings and common-setting IDs."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from aquaclean_console_app.aquaclean_core.Clients.AquaCleanClient import AquaCleanClient


def _client_with_calls():
    """Construct only the part of the client needed to test BLE writes."""
    calls = []

    async def set_active(setting_id, value):
        calls.append(("active", setting_id, value))

    async def set_stored(setting_id, value):
        calls.append(("stored", setting_id, value))

    client = object.__new__(AquaCleanClient)
    client.base_client = SimpleNamespace(
        set_active_common_setting_async=set_active,
        set_stored_common_setting_async=set_stored,
    )
    return client, calls


@pytest.mark.parametrize("setting_id, value", [(1, 4), (2, 6), (3, 2)])
async def test_orientation_light_writes_active_before_stored(setting_id, value):
    client, calls = _client_with_calls()

    await client.set_stored_common_setting(setting_id, value)

    assert calls == [("active", setting_id, value), ("stored", setting_id, value)]


@pytest.mark.parametrize("setting_id, value", [(0, 1), (4, 2), (6, 1), (7, 0)])
async def test_other_common_settings_only_write_stored(setting_id, value):
    client, calls = _client_with_calls()

    await client.set_stored_common_setting(setting_id, value)

    assert calls == [("stored", setting_id, value)]


async def test_active_write_failure_does_not_store_value():
    client, calls = _client_with_calls()

    async def failed_active(setting_id, value):
        calls.append(("active", setting_id, value))
        raise RuntimeError("active write failed")

    client.base_client.set_active_common_setting_async = failed_active

    with pytest.raises(RuntimeError, match="active write failed"):
        await client.set_stored_common_setting(2, 6)

    assert calls == [("active", 2, 6)]


async def test_stored_write_failure_is_propagated():
    client, calls = _client_with_calls()

    async def failed_stored(setting_id, value):
        calls.append(("stored", setting_id, value))
        raise RuntimeError("stored write failed")

    client.base_client.set_stored_common_setting_async = failed_stored

    with pytest.raises(RuntimeError, match="stored write failed"):
        await client.set_stored_common_setting(3, 1)

    assert calls == [("active", 3, 1), ("stored", 3, 1)]


def _main_constant(name):
    """Parse configuration dictionaries without importing main.py (starts services)."""
    path = Path(__file__).resolve().parents[1] / "aquaclean_console_app" / "main.py"
    module = ast.parse(path.read_text(encoding="utf-8"))
    assignments = [
        stmt.value
        for stmt in module.body
        if isinstance(stmt, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in stmt.targets)
    ]
    assert len(assignments) == 1, f"{name} must only be defined once"
    return ast.literal_eval(assignments[0])


def test_mqtt_common_setting_names_are_correct():
    assert _main_constant("_COMMON_SETTING_MQTT_KEYS") == {
        0: "odourExtractionRunOn",
        1: "orientationLightBrightness",
        2: "orientationLightColor",
        3: "orientationLightActivation",
        4: "wcLidSensorSensitivity",
        6: "wcLidOpenAutomatically",
        7: "wcLidCloseAutomatically",
    }


def test_common_setting_ranges_are_correct():
    assert _main_constant("_COMMON_SETTING_RANGES") == {
        0: (0, 1),
        1: (0, 4),
        2: (0, 6),
        3: (0, 2),
        4: (0, 4),
        6: (0, 1),
        7: (0, 1),
    }

def test_profile_setting_ranges_are_correct():
    """Check all profile values, especially ID 13 (dryer spray intensity)."""
    assert _main_constant("_PROFILE_SETTING_RANGES") == {
        0: (0, 1),
        1: (0, 1),
        2: (0, 4),
        3: (0, 4),
        4: (0, 4),
        5: (0, 4),
        6: (0, 5),
        7: (0, 5),
        8: (0, 5),
        9: (0, 1),
        13: (0, 4),
    }

