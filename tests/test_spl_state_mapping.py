"""Execute real mapping methods with recorded SPL states, without BLE/HA imports.

Run: python -m unittest discover -s tests -p test_spl_state_mapping.py
"""
import ast
import copy
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
CLIENT = "aquaclean_console_app/aquaclean_core/Clients/AquaCleanClient.py"
MAIN = "aquaclean_console_app/main.py"
COORDINATOR = "custom_components/geberit_aquaclean/coordinator.py"
ALBA = "aquaclean_console_app/aquaclean_core/Clients/AlbaBaseClient.py"


def module(path):
    return ast.parse((ROOT / path).read_text(encoding="utf-8"))


def method(path, class_name, name, **namespace):
    cls = next(n for n in module(path).body if isinstance(n, ast.ClassDef) and n.name == class_name)
    node = copy.deepcopy(next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name))
    # Imports are not part of the state mapping; avoid optional BLE/HA dependencies.
    node.body = [n for n in node.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    exec(compile(ast.Module(body=[node], type_ignores=[]), path, "exec"), namespace)
    return namespace[name]


def spl_client(values):
    async def get(params):
        return NS(data_array=[values[p] if p < len(values) else 0 for p in params])
    return NS(base_client=NS(get_system_parameter_list_async=AsyncMock(side_effect=get)))


class SPLMappingTests(unittest.IsolatedAsyncioTestCase):
    # #45: idle, isolated dryer, full shower/dryer cycle; #52: Sela anal/lady.
    CASES = [
        ([1, 0, 0, 0], False, False),
        ([1, 0, 0, 1], False, True),
        ([1, 1, 0, 1], True, True),
        ([1, 1, 0, 0], True, False),
        ([1, 0, 1, 0], False, False),
        ([1, 1280, 0, 0], True, False),  # packed state: don't test only bit 0
    ]

    async def test_persistent_poll_and_partial_events(self):
        poll = method(CLIENT, "AquaCleanClient", "_state_changed_timer_elapsed",
                      SPL_PARAMS_MERA_COMFORT_STATE=list(range(8)),
                      SPL_PARAMS_MERA_COMFORT_AUX=[12, 13], DeviceStateChangedEventArgs=NS)
        self_obj = NS(last_device_state_changed_event_args=None,
                      DeviceStateChanged=NS(invoke_async=AsyncMock()))
        for raw, anal, dryer in self.CASES:
            with self.subTest(raw=raw):
                self_obj.base_client = spl_client(raw).base_client
                self_obj.last_device_state_changed_event_args = None
                await poll(self_obj)
                event = self_obj.DeviceStateChanged.invoke_async.call_args.args[1]
                self.assertEqual((event.IsAnalShowerRunning, event.IsDryerRunning), (anal, dryer))
                self.assertEqual(event.IsLadyShowerRunning, bool(raw[2]))
                self.assertEqual(self_obj.base_client.get_system_parameter_list_async.call_args_list[0].args, (list(range(8)),))
                await poll(self_obj)
                count = self_obj.DeviceStateChanged.invoke_async.await_count
                await poll(self_obj)
                self.assertEqual(self_obj.DeviceStateChanged.invoke_async.await_count, count)
        self_obj.base_client = spl_client([1, 0, 0, 1]).base_client
        await poll(self_obj)
        event = self_obj.DeviceStateChanged.invoke_async.call_args.args[1]
        self.assertFalse(event.IsAnalShowerRunning)
        self.assertTrue(event.IsDryerRunning)

    async def test_ondemand_full_state(self):
        fetch = method(MAIN, "ApiMode", "_fetch_state",
                       SPL_PARAMS_MERA_COMFORT_STATE=list(range(8)), SPL_PARAMS_MERA_COMFORT_AUX=[12, 13])
        for raw, anal, dryer in self.CASES:
            with self.subTest(raw=raw):
                owner = NS(service=NS(device_state={}))
                result = await fetch(owner, spl_client(raw), _skip_profile=True)
                self.assertEqual((result["is_anal_shower_running"], result["is_dryer_running"]), (anal, dryer))
                self.assertEqual(owner.service.device_state["is_anal_shower_running"], anal)

    async def test_rest_single_queries_and_cached_state(self):
        for name, key, index in [("_fetch_anal_shower_state", "is_anal_shower_running", 1),
                                 ("_fetch_dryer_state", "is_dryer_running", 3)]:
            fetch = method(MAIN, "ApiMode", name)
            for raw, anal, dryer in self.CASES:
                with self.subTest(name=name, raw=raw):
                    owner = NS(service=NS(device_state={}))
                    client = spl_client(raw)
                    result = await fetch(owner, client)
                    self.assertEqual(result[key], bool(raw[index]))
                    self.assertEqual(owner.service.device_state[key], bool(raw[index]))
                    client.base_client.get_system_parameter_list_async.assert_awaited_once_with([index])

    async def test_persistent_mapping_reaches_existing_mqtt_topics(self):
        poll = method(CLIENT, "AquaCleanClient", "_state_changed_timer_elapsed",
                      SPL_PARAMS_MERA_COMFORT_STATE=list(range(8)),
                      SPL_PARAMS_MERA_COMFORT_AUX=[12, 13], DeviceStateChangedEventArgs=NS)
        handler = method(MAIN, "ServiceMode", "on_device_state_changed")
        owner = NS(mqttConfig={"topic": "test"}, device_state={},
                   mqtt_service=NS(send_data_async=AsyncMock()), on_state_updated=None)

        async def publish(sender, event):
            await handler(owner, sender, event)

        client = NS(base_client=spl_client([1, 0, 0, 1]).base_client,
                    last_device_state_changed_event_args=None, DeviceStateChanged=NS(invoke_async=publish))
        await poll(client)
        owner.mqtt_service.send_data_async.assert_any_await("test/peripheralDevice/monitor/isAnalShowerRunning", "False")
        owner.mqtt_service.send_data_async.assert_any_await("test/peripheralDevice/monitor/isDryerRunning", "True")

    async def test_cli_status_and_single_queries(self):
        # Execute the actual command dispatch subtree, without connection setup.
        cli = next(n for n in module(MAIN).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_cli")
        dispatch = next(n for n in ast.walk(cli) if isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                        and ast.unparse(n.test) == "args.command in ('status', 'system-parameters')")
        function = ast.parse("async def query(args, client, result):\n    pass\n").body[0]
        function.body = [dispatch]
        namespace = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), MAIN, "exec"), namespace)
        for raw, anal, dryer in self.CASES:
            for command in ["status", "system-parameters", "anal-shower-state", "lady-shower-state", "dryer-state"]:
                with self.subTest(raw=raw, command=command):
                    result = {}
                    await namespace["query"](NS(command=command), spl_client(raw), result)
                    expected = {"is_user_sitting": bool(raw[0]), "is_anal_shower_running": anal,
                                "is_lady_shower_running": bool(raw[2]), "is_dryer_running": dryer}
                    for key, value in result["data"].items():
                        self.assertEqual(value, expected[key])

    async def test_hacs_mera_state(self):
        # The real coordinator method uses cached settings; other reads are fakes.
        cls = next(n for n in module(COORDINATOR).body if isinstance(n, ast.ClassDef))
        fetch = method(COORDINATOR, cls.name, "_build_mera_result")
        for raw, anal, dryer in self.CASES:
            with self.subTest(raw=raw):
                client = spl_client(raw)
                base = client.base_client
                base.get_device_identification_async = AsyncMock(return_value=NS(sap_number="", serial_number="", production_date="", description=""))
                base.get_device_initial_operation_date = AsyncMock(return_value="")
                base.get_statistics_descale_async = AsyncMock(return_value=NS(days_until_next_descale=0,
                    days_until_shower_restricted=0, shower_cycles_until_confirmation=0, number_of_descale_cycles=0,
                    date_time_at_last_descale=0, unposted_shower_cycles=0))
                for name in ["get_soc_application_versions_async", "get_firmware_version_list_async", "get_filter_status_async"]:
                    setattr(base, name, AsyncMock(return_value={}))
                owner = NS(_mera_profile_settings_cache={}, _mera_common_settings_cache={})
                result = await fetch(owner, client)
                self.assertEqual((result["is_anal_shower_running"], result["is_dryer_running"]), (anal, dryer))

    async def test_alba_adapter_normalizes_enums_and_short_queries(self):
        dp = NS(DP_USER_DETECTION_STATUS=607, DP_ANAL_SHOWER_STATUS=564, DP_LADY_SHOWER_STATUS=872)
        namespace = {"DpId": dp, "Optional": __import__("typing").Optional}
        assignment = next(n for n in module(ALBA).body if isinstance(n, ast.AnnAssign)
                          and isinstance(n.target, ast.Name) and n.target.id == "_SPL_DPID")
        exec(compile(ast.Module(body=[assignment], type_ignores=[]), ALBA, "exec"), namespace)
        fetch = method(ALBA, "AlbaBaseClient", "get_system_parameter_list_async",
                       **namespace, struct=struct, SystemParameterList=NS)
        for status in range(8):
            with self.subTest(status=status):
                owner = NS(_ble20=NS(poll_state=AsyncMock(return_value={607: b"\x01", 564: bytes([status])})))
                full = await fetch(owner, [0, 1, 2, 3])
                self.assertEqual(full.data_array[:4], [1, int(status >= 3), 0, 0])
                for params, expected in [([1], int(status >= 3)), ([3], 0)]:
                    result = await fetch(owner, params)
                    self.assertEqual(result.data_array[0], expected)

    async def test_hacs_alba_reads_updated_adapter_layout(self):
        cls = next(n for n in module(COORDINATOR).body if isinstance(n, ast.ClassDef))
        fetch = method(COORDINATOR, cls.name, "_build_alba_result", _ALBA_SLOW_POLL_EVERY=10)
        ident = NS(sap_number="", serial_number="", production_date="", description="")
        owner = NS(_alba_poll_num=1, _alba_slow_cache={"ident": ident, "misc": {},
                   "instanced": {}, "profile_settings": {}})
        for active in [0, 1]:
            client = spl_client([1, active, 0, 0])
            client.firmware_versions = {}
            client.base_client.get_misc_state_fast_async = AsyncMock(return_value={})
            result = await fetch(owner, client)
            self.assertEqual(result["is_anal_shower_running"], bool(active))
            self.assertFalse(result["is_dryer_running"])


if __name__ == "__main__":
    unittest.main()
