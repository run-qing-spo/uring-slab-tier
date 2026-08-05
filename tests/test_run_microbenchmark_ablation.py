import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).parents[1] / "scripts" / "run_microbenchmark_ablation.py"
_SPEC = importlib.util.spec_from_file_location("run_microbenchmark_ablation", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_five_repetitions_balance_arm_positions() -> None:
    orders = [_MODULE._arm_order(index) for index in range(5)]
    for position in range(5):
        assert {order[position] for order in orders} == set(_MODULE.ARMS)
