import ast
from pathlib import Path


CLIENT_PATH = (
    Path(__file__).resolve().parents[1]
    / "aquaclean_console_app"
    / "aquaclean_core"
    / "Clients"
    / "AquaCleanClient.py"
)
SOURCE = CLIENT_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _assignment_value(name: str):
    for node in TREE.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"assignment {name!r} not found")


def _method_source(class_name: str, method_name: str) -> str:
    cls = next(
        node
        for node in TREE.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    method = next(
        node
        for node in cls.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == method_name
    )
    return ast.get_source_segment(SOURCE, method) or ""


def test_mera_spl_batches_stay_within_safe_boundary():
    assert _assignment_value("SPL_PARAMS_MERA_COMFORT_STATE") == list(range(8))
    assert _assignment_value("SPL_PARAMS_MERA_COMFORT_OFFSETS") == [12, 13]
    assert len(_assignment_value("SPL_PARAMS_MERA_COMFORT_STATE")) <= 8
    assert len(_assignment_value("SPL_PARAMS_MERA_COMFORT_OFFSETS")) <= 8


def test_state_poll_uses_two_getspl_requests_and_maps_offsets_from_second_result():
    source = _method_source("AquaCleanClient", "_state_changed_timer_elapsed")

    assert source.count("get_system_parameter_list_async(") == 2
    assert "SPL_PARAMS_MERA_COMFORT_STATE" in source
    assert "SPL_PARAMS_MERA_COMFORT_OFFSETS" in source

    assert "IsUserSitting=state_result.data_array[0] != 0" in source
    assert "IsAnalShowerRunning=state_result.data_array[3] != 0" in source
    assert "IsLadyShowerRunning=state_result.data_array[2] != 0" in source
    assert "IsDryerRunning=state_result.data_array[1] != 0" in source

    assert "LidOffsetPosition=offset_result.data_array[0]" in source
    assert "ShowerArmOffsetPosition=offset_result.data_array[1]" in source

    # Regression guard: never reintroduce the known-destructive combined batch.
    assert "[0, 1, 2, 3, 4, 5, 6, 7, 12, 13]" not in source


if __name__ == "__main__":
    test_mera_spl_batches_stay_within_safe_boundary()
    test_state_poll_uses_two_getspl_requests_and_maps_offsets_from_second_result()
    print("split SPL regression checks: OK")
