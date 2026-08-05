import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_fs_m0.py"
_SPEC = importlib.util.spec_from_file_location("benchmark_fs_m0", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_percentile_interpolates() -> None:
    assert _MODULE._percentile([0.0, 10.0], 0.95) == 9.5


def test_block_path_is_stable_and_sharded(tmp_path: Path) -> None:
    first = Path(_MODULE._block_path(tmp_path, 7, 3))
    second = Path(_MODULE._block_path(tmp_path, 7, 3))
    other = Path(_MODULE._block_path(tmp_path, 7, 4))

    assert first == second
    assert first != other
    assert first.suffix == ".bin"
    assert first.is_relative_to(tmp_path)
    assert len(first.relative_to(tmp_path).parts) == 3
