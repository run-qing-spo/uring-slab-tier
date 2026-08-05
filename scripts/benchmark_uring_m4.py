#!/usr/bin/env python3
"""M4微基准：直接驱动当前uring-slab C++ DataEngine。"""

from __future__ import annotations

import argparse
import json
import mmap
import os
import platform
import resource
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _sample_summary(samples: list[int]) -> dict[str, Any]:
    return {
        "mean": statistics.mean(samples),
        "median": statistics.median(samples),
        "p95": _percentile(samples, 0.95),
        "p99": _percentile(samples, 0.99),
        "max": max(samples),
        "samples": samples,
    }


def _usage_delta(before: resource.struct_rusage,
                 after: resource.struct_rusage) -> dict[str, float | int]:
    return {
        "user_cpu_seconds": after.ru_utime - before.ru_utime,
        "system_cpu_seconds": after.ru_stime - before.ru_stime,
        "voluntary_context_switches": after.ru_nvcsw - before.ru_nvcsw,
        "involuntary_context_switches": after.ru_nivcsw - before.ru_nivcsw,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="当前uring-slab M4 load/store/mixed微基准"
    )
    parser.add_argument(
        "--direction", choices=("load", "store", "mixed"), required=True
    )
    parser.add_argument("--engine", choices=("uring", "blocking"), default="uring")
    parser.add_argument("--root-dir", type=Path, required=True)
    parser.add_argument("--block-size-bytes", type=int, required=True)
    parser.add_argument("--jobs", type=int, default=128)
    parser.add_argument("--blocks-per-job", type=int, default=8)
    parser.add_argument("--qps", type=float, default=100.0)
    parser.add_argument("--read-qps", type=float, default=50.0)
    parser.add_argument("--write-qps", type=float, default=50.0)
    parser.add_argument("--write-start-offset-ms", type=float, default=10.0)
    parser.add_argument("--total-qd", type=int, default=128)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--pending-capacity", type=int, default=4096)
    parser.add_argument(
        "--submit-batch-size", type=int, default=0,
        help="0表示M4尽可能批量；1表示M3逐SQE submit",
    )
    parser.add_argument("--max-inflight-jobs", type=int, default=32)
    parser.add_argument("--poll-interval-us", type=float, default=100.0)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


def _validate_args(args: argparse.Namespace) -> None:
    for name, value in {
        "block-size-bytes": args.block_size_bytes,
        "jobs": args.jobs,
        "blocks-per-job": args.blocks_per_job,
        "total-qd": args.total_qd,
        "pending-capacity": args.pending_capacity,
        "max-inflight-jobs": args.max_inflight_jobs,
    }.items():
        if value <= 0:
            raise ValueError(f"--{name}必须大于0，得到{value}")
    if args.workers < 4:
        raise ValueError("--workers必须至少为4")
    if args.submit_batch_size < 0 or args.submit_batch_size > args.total_qd:
        raise ValueError("--submit-batch-size必须在0到total_qd之间")
    if args.total_qd < 4:
        raise ValueError("--total-qd必须至少为4")
    if args.pending_capacity < args.total_qd:
        raise ValueError("--pending-capacity不能小于--total-qd")
    for name, value in {
        "qps": args.qps,
        "read-qps": args.read_qps,
        "write-qps": args.write_qps,
        "write-start-offset-ms": args.write_start_offset_ms,
        "poll-interval-us": args.poll_interval_us,
    }.items():
        if value < 0:
            raise ValueError(f"--{name}不能小于0，得到{value}")
    if args.direction == "mixed" and (args.read_qps == 0 or args.write_qps == 0):
        raise ValueError("mixed的read/write QPS必须大于0")
    if args.block_size_bytes % mmap.PAGESIZE != 0:
        raise ValueError(
            "--block-size-bytes必须是系统页大小的整数倍："
            f"block={args.block_size_bytes}, page={mmap.PAGESIZE}"
        )
    if platform.system() != "Linux" or not hasattr(os, "O_DIRECT"):
        raise RuntimeError("正式M4基准只能在支持O_DIRECT/io_uring的Linux运行")


def _build_schedule(args: argparse.Namespace) -> tuple[
    tuple[str, ...], list[tuple[int, int, str, int, int]]
]:
    directions = ("load", "store") if args.direction == "mixed" else (args.direction,)
    schedule: list[tuple[int, int, str, int, int]] = []
    next_job_id = 0
    for direction in directions:
        qps = (
            args.read_qps
            if args.direction == "mixed" and direction == "load"
            else args.write_qps
            if args.direction == "mixed"
            else args.qps
        )
        interval_ns = 0 if qps == 0 else round(1_000_000_000 / qps)
        start_offset_ns = (
            round(args.write_start_offset_ms * 1_000_000)
            if args.direction == "mixed" and direction == "store"
            else 0
        )
        priority = 0 if direction == "load" else 1
        for job_index in range(args.jobs):
            schedule.append(
                (
                    start_offset_ns + job_index * interval_ns,
                    priority,
                    direction,
                    job_index,
                    next_job_id,
                )
            )
            next_job_id += 1
    schedule.sort()
    return directions, schedule


def _secondary_slot(
    direction_base: int,
    job_index: int,
    block_index: int,
    blocks_per_job: int,
) -> int:
    return direction_base + job_index * blocks_per_job + block_index


def _submit_job(
    engine: Any,
    *,
    direction: str,
    job_id: int,
    job_index: int,
    slot_group: int,
    secondary_base: int,
    blocks_per_job: int,
) -> None:
    submit = engine.submit_load if direction == "load" else engine.submit_store
    primary_base = slot_group * blocks_per_job
    for block_index in range(blocks_per_job):
        key = f"{direction}:{job_index}:{block_index}".encode()
        submit(
            job_id,
            key,
            primary_base + block_index,
            _secondary_slot(
                secondary_base, job_index, block_index, blocks_per_job
            ),
        )


def _seed_loads(engine: Any, *, jobs: int, blocks_per_job: int) -> None:
    expected = jobs * blocks_per_job
    for job_index in range(jobs):
        _submit_job(
            engine,
            direction="store",
            job_id=10_000_000 + job_index,
            job_index=job_index,
            slot_group=0,
            secondary_base=0,
            blocks_per_job=blocks_per_job,
        )
    completions = engine.drain()
    if len(completions) != expected:
        raise RuntimeError(
            f"seed completion数不符：expected={expected}, got={len(completions)}"
        )
    failed = [item for item in completions if not item[2]]
    if failed:
        raise RuntimeError(f"seed store失败：{failed[:3]}")


def _stats_delta(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    return {key: int(after[key]) - int(before.get(key, 0)) for key in after}


def _arm_name(engine: str, submit_batch_size: int) -> str:
    if engine == "blocking":
        return "m2"
    return "m3" if submit_batch_size == 1 else "m4"


def _run(args: argparse.Namespace) -> dict[str, Any]:
    try:
        from uring_slab_tier import _uring_slab_engine
    except ImportError as exc:
        raise RuntimeError("_uring_slab_engine未编译或不在当前Python环境") from exc

    directions, schedule = _build_schedule(args)
    total_jobs = len(schedule)
    slot_groups = min(total_jobs, args.max_inflight_jobs)
    total_primary_slots = slot_groups * args.blocks_per_job
    buffer_bytes = total_primary_slots * args.block_size_bytes
    primary_mmap = mmap.mmap(-1, buffer_bytes)
    flat_view = memoryview(primary_mmap)
    for offset in range(0, buffer_bytes, mmap.PAGESIZE):
        flat_view[offset] = 0xA5
    primary_view = flat_view.cast(
        "B", shape=[total_primary_slots, args.block_size_bytes]
    )

    arm = _arm_name(args.engine, args.submit_batch_size)
    run_id = args.run_id or f"{args.direction}-{uuid.uuid4().hex[:12]}"
    run_root = args.root_dir.resolve() / f"{arm}-{run_id}"
    if run_root.exists():
        primary_view.release()
        flat_view.release()
        primary_mmap.close()
        raise FileExistsError(f"run目录已存在，拒绝复用：{run_root}")
    run_root.mkdir(parents=True)
    slab_path = run_root / "slab.bin"
    secondary_slots = len(directions) * args.jobs * args.blocks_per_job
    slab_bytes = secondary_slots * args.block_size_bytes
    if args.engine == "blocking":
        engine = _uring_slab_engine.BlockingDataEngine(
            primary_view,
            str(slab_path),
            slab_bytes,
            workers=args.workers,
            pending_capacity=args.pending_capacity,
        )
    else:
        engine = _uring_slab_engine.DataEngine(
            primary_view,
            str(slab_path),
            slab_bytes,
            total_qd=args.total_qd,
            pending_capacity=args.pending_capacity,
            submit_batch_size=args.submit_batch_size,
        )

    try:
        if "load" in directions:
            _seed_loads(engine, jobs=args.jobs, blocks_per_job=args.blocks_per_job)
        engine.reset_stats()
        stats_before = dict(engine.stats_snapshot())

        free_groups = list(range(slot_groups - 1, -1, -1))
        job_groups: dict[int, int] = {}
        job_directions: dict[int, str] = {}
        remaining = {job_id: args.blocks_per_job for *_, job_id in schedule}
        submitted_at_ns: dict[int, int] = {}
        completion_ns: dict[int, int] = {}
        submit_call_ns = {direction: [] for direction in directions}
        dispatch_lag_ns = {direction: [] for direction in directions}
        max_inflight_by_direction = {direction: 0 for direction in directions}
        max_inflight_observed = 0
        failed_blocks: list[tuple[Any, ...]] = []
        next_event = 0
        completed_jobs = 0
        poll_seconds = args.poll_interval_us / 1_000_000.0

        usage_before = resource.getrusage(resource.RUSAGE_SELF)
        wall_start_ns = time.perf_counter_ns()
        while completed_jobs < total_jobs:
            now_ns = time.perf_counter_ns()
            while (
                next_event < total_jobs
                and free_groups
                and now_ns >= wall_start_ns + schedule[next_event][0]
            ):
                due_offset_ns, _, direction, job_index, job_id = schedule[next_event]
                group = free_groups.pop()
                submit_start_ns = time.perf_counter_ns()
                _submit_job(
                    engine,
                    direction=direction,
                    job_id=job_id,
                    job_index=job_index,
                    slot_group=group,
                    secondary_base=(
                        args.jobs * args.blocks_per_job
                        if args.direction == "mixed" and direction == "store"
                        else 0
                    ),
                    blocks_per_job=args.blocks_per_job,
                )
                submit_end_ns = time.perf_counter_ns()
                submitted_at_ns[job_id] = submit_start_ns
                submit_call_ns[direction].append(submit_end_ns - submit_start_ns)
                dispatch_lag_ns[direction].append(
                    max(0, submit_start_ns - (wall_start_ns + due_offset_ns))
                )
                job_groups[job_id] = group
                job_directions[job_id] = direction
                next_event += 1
                max_inflight_observed = max(max_inflight_observed, len(job_groups))
                for current_direction in directions:
                    inflight = sum(
                        value == current_direction for value in job_directions.values()
                    )
                    max_inflight_by_direction[current_direction] = max(
                        max_inflight_by_direction[current_direction], inflight
                    )
                now_ns = submit_end_ns

            completions = engine.poll_completions()
            observed_ns = time.perf_counter_ns()
            for completion in completions:
                job_id, _key, success, _error_code = completion
                if job_id not in remaining or remaining[job_id] <= 0:
                    raise RuntimeError(f"未知或重复block completion：job_id={job_id}")
                if not success:
                    failed_blocks.append(completion)
                remaining[job_id] -= 1
                if remaining[job_id] == 0:
                    completion_ns[job_id] = observed_ns
                    free_groups.append(job_groups.pop(job_id))
                    job_directions.pop(job_id)
                    completed_jobs += 1

            if completed_jobs < total_jobs:
                time.sleep(poll_seconds if poll_seconds else 0)

        wall_end_ns = time.perf_counter_ns()
        usage_after = resource.getrusage(resource.RUSAGE_SELF)
        stats_after = dict(engine.stats_snapshot())
    finally:
        engine.shutdown()
        primary_view.release()
        flat_view.release()
        primary_mmap.close()

    if failed_blocks:
        raise RuntimeError(f"M4 block I/O失败：{failed_blocks[:3]}")

    scheduled_direction = {job_id: direction for _, _, direction, _, job_id in schedule}
    wall_seconds = (wall_end_ns - wall_start_ns) / 1_000_000_000
    direction_results = {}
    for direction in directions:
        latencies = [
            completion_ns[job_id] - submitted_at_ns[job_id]
            for job_id in range(total_jobs)
            if scheduled_direction[job_id] == direction
        ]
        blocks = args.jobs * args.blocks_per_job
        byte_count = blocks * args.block_size_bytes
        direction_results[direction] = {
            "completed_jobs": args.jobs,
            "failed_jobs": 0,
            "blocks": blocks,
            "bytes": byte_count,
            "jobs_per_second_over_window": args.jobs / wall_seconds,
            "blocks_per_second_over_window": blocks / wall_seconds,
            "bytes_per_second_over_window": byte_count / wall_seconds,
            "max_inflight_jobs_observed": max_inflight_by_direction[direction],
            "job_latency_ns": _sample_summary(latencies),
            "submit_call_ns": _sample_summary(submit_call_ns[direction]),
            "dispatch_lag_ns": _sample_summary(dispatch_lag_ns[direction]),
        }

    total_blocks = total_jobs * args.blocks_per_job
    total_bytes = total_blocks * args.block_size_bytes
    return {
        "schema_version": 2,
        "benchmark": f"uring_slab_{arm}",
        "run_id": run_id,
        "run_root": str(run_root),
        "valid_for_formal_comparison": True,
        "configuration": {
            "direction": args.direction,
            "block_size_bytes": args.block_size_bytes,
            "jobs": args.jobs,
            "blocks_per_job": args.blocks_per_job,
            "offered_qps": args.qps if args.direction != "mixed" else None,
            "read_qps": args.read_qps if args.direction == "mixed" else None,
            "write_qps": args.write_qps if args.direction == "mixed" else None,
            "write_start_offset_ms": (
                args.write_start_offset_ms if args.direction == "mixed" else None
            ),
            "total_qd": args.total_qd,
            "engine": args.engine,
            "workers": args.workers if args.engine == "blocking" else None,
            "pending_capacity": args.pending_capacity,
            "submit_batch_size": args.submit_batch_size,
            "max_inflight_jobs": args.max_inflight_jobs,
            "effective_slot_groups": slot_groups,
            "poll_interval_us": args.poll_interval_us,
            "buffer_bytes": buffer_bytes,
            "slab_bytes": slab_bytes,
            "o_direct": True,
            "batch_submit": (
                args.submit_batch_size != 1 if args.engine == "uring" else None
            ),
        },
        "result": {
            "completed_jobs": total_jobs,
            "failed_jobs": 0,
            "blocks": total_blocks,
            "bytes": total_bytes,
            "wall_seconds": wall_seconds,
            "jobs_per_second": total_jobs / wall_seconds,
            "blocks_per_second": total_blocks / wall_seconds,
            "bytes_per_second": total_bytes / wall_seconds,
            "max_inflight_jobs_observed": max_inflight_observed,
            "directions": direction_results,
            "engine_stats_delta": _stats_delta(stats_after, stats_before),
            "process_usage": _usage_delta(usage_before, usage_after),
        },
    }


def main() -> None:
    args = _parse_args()
    _validate_args(args)
    result = _run(args)
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"M4 benchmark failed: {exc}", file=sys.stderr)
        raise
