import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_fs_m0.py"
_SPEC = importlib.util.spec_from_file_location("benchmark_slab_m1", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_mixed_slab_offsets_do_not_overlap() -> None:
    block_size = 4096
    blocks_per_job = 8
    jobs = 128
    last_load = _MODULE._secondary_offset(
        0, jobs - 1, blocks_per_job - 1, blocks_per_job, block_size
    )
    first_store = _MODULE._secondary_offset(
        jobs * blocks_per_job, 0, 0, blocks_per_job, block_size
    )
    assert first_store - last_load == block_size
