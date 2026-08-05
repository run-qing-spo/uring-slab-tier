import importlib.util
from pathlib import Path
from types import SimpleNamespace


_SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_uring_m4.py"
_SPEC = importlib.util.spec_from_file_location("benchmark_uring_m4", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_mixed_schedule_is_interleaved() -> None:
    args = SimpleNamespace(
        direction="mixed",
        jobs=2,
        qps=100.0,
        read_qps=50.0,
        write_qps=50.0,
        write_start_offset_ms=10.0,
    )
    directions, schedule = _MODULE._build_schedule(args)
    assert directions == ("load", "store")
    assert [(item[0], item[2]) for item in schedule] == [
        (0, "load"),
        (10_000_000, "store"),
        (20_000_000, "load"),
        (30_000_000, "store"),
    ]


def test_secondary_slot_applies_direction_base() -> None:
    assert _MODULE._secondary_slot(0, 3, 7, 8) == 31
    assert _MODULE._secondary_slot(1024, 0, 0, 8) == 1024


def test_submit_batch_size_validation() -> None:
    args = SimpleNamespace(
        direction="load",
        block_size_bytes=4096,
        jobs=1,
        blocks_per_job=1,
        total_qd=8,
        pending_capacity=8,
        submit_batch_size=9,
        max_inflight_jobs=1,
        qps=1.0,
        read_qps=1.0,
        write_qps=1.0,
        write_start_offset_ms=0.0,
        poll_interval_us=0.0,
    )
    try:
        _MODULE._validate_args(args)
    except ValueError as exc:
        assert "submit-batch-size" in str(exc)
    else:
        raise AssertionError("超出QD的submit batch必须被拒绝")
