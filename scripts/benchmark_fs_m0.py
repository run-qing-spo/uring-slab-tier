#!/usr/bin/env python3
"""M0 微基准：复用 vLLM 原生 FS 数据面测试纯 load/store/mixed。

本脚本不构造 tier control plane，也不执行 lookup。它直接复用：

* vllm.v1.kv_offload.tiering.fs.io.store_block/load_block
* vllm.v1.kv_offload.tiering.fs.thread_pool.DualQueueThreadPool

因此保留原生 FS 的 per-block 文件、O_DIRECT、open/read/write/close、store
临时文件加 rename、双队列线程池和 job completion 聚合语义。
"""

from __future__ import annotations

import argparse
import fcntl
import functools
import hashlib
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


def _block_path(root: Path, job_id: int, block_index: int) -> str:
    """生成与原生 FS 相同层级形状的稳定 per-block 路径。"""
    digest = hashlib.sha256(f"m0:{job_id}:{block_index}".encode()).hexdigest()
    return str(root / digest[:3] / f"{digest[3:5]}_g0" / f"{digest}.bin")


def _secondary_offset(
    direction_base_slots: int,
    job_index: int,
    block_index: int,
    blocks_per_job: int,
    block_size: int,
) -> int:
    slot = direction_base_slots + job_index * blocks_per_job + block_index
    return slot * block_size


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
        description="Python pool的M0 files / M1 slab微基准"
    )
    parser.add_argument(
        "--direction", choices=("load", "store", "mixed"), required=True
    )
    parser.add_argument("--layout", choices=("files", "slab"), default="files")
    parser.add_argument("--root-dir", type=Path, required=True)
    parser.add_argument("--block-size-bytes", type=int, required=True)
    parser.add_argument(
        "--jobs", type=int, default=128,
        help="每个启用方向的 job 数；mixed 会提交该数量的 load 和 store",
    )
    parser.add_argument("--blocks-per-job", type=int, default=8)
    parser.add_argument("--qps", type=float, default=100.0,
                        help="纯 load/store 的 job 提交速率；0 表示尽快提交")
    parser.add_argument("--read-qps", type=float, default=50.0,
                        help="mixed 的 load job 提交速率")
    parser.add_argument("--write-qps", type=float, default=50.0,
                        help="mixed 的 store job 提交速率")
    parser.add_argument(
        "--write-start-offset-ms", type=float, default=10.0,
        help="mixed 中 write 流相对 read 流的启动偏移",
    )
    parser.add_argument("--n-read-threads", type=int, default=16)
    parser.add_argument("--n-write-threads", type=int, default=16)
    parser.add_argument("--max-inflight-jobs", type=int, default=32,
                        help="同时占用独立 primary slot 组的 job 上限")
    parser.add_argument("--poll-interval-us", type=float, default=100.0)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--allow-buffered-dev",
        action="store_true",
        help="仅供非 Linux 开发检查；正式结果禁止使用",
    )
    return parser.parse_args()


def _validate_args(args: argparse.Namespace) -> None:
    positive = {
        "block-size-bytes": args.block_size_bytes,
        "jobs": args.jobs,
        "blocks-per-job": args.blocks_per_job,
        "max-inflight-jobs": args.max_inflight_jobs,
    }
    for name, value in positive.items():
        if value <= 0:
            raise ValueError(f"--{name} 必须大于 0，得到 {value}")
    if args.n_read_threads < 0 or args.n_write_threads < 0:
        raise ValueError("read/write线程数不能小于0")
    if args.n_read_threads + args.n_write_threads == 0:
        raise ValueError("read/write线程总数必须大于0")
    for name, value in {
        "qps": args.qps,
        "read-qps": args.read_qps,
        "write-qps": args.write_qps,
        "write-start-offset-ms": args.write_start_offset_ms,
    }.items():
        if value < 0:
            raise ValueError(f"--{name} 不能小于 0，得到 {value}")
    if args.direction == "mixed" and (args.read_qps == 0 or args.write_qps == 0):
        raise ValueError("mixed 的 --read-qps 和 --write-qps 必须大于0")
    if args.poll_interval_us < 0:
        raise ValueError("--poll-interval-us 不能小于 0")
    if args.block_size_bytes % mmap.PAGESIZE != 0:
        raise ValueError(
            "--block-size-bytes 必须是系统页大小的整数倍："
            f"block={args.block_size_bytes}, page={mmap.PAGESIZE}"
        )
    direct_available = platform.system() == "Linux" and hasattr(os, "O_DIRECT")
    if not direct_available and not args.allow_buffered_dev:
        raise RuntimeError(
            "正式 M0 基准要求 Linux O_DIRECT；开发检查可显式传 "
            "--allow-buffered-dev，但结果不可用于实验"
        )


def _build_tasks(
    *,
    operation: Any,
    paths: list[Any],
    buffer: memoryview,
    slot_group: int,
    blocks_per_job: int,
    block_size: int,
):
    base_slot = slot_group * blocks_per_job
    return (
        functools.partial(
            operation,
            path,
            buffer,
            (base_slot + block_index) * block_size,
            block_size,
        )
        for block_index, path in enumerate(paths)
    )


def _drain_seed_completions(pool: Any, expected_jobs: int) -> None:
    pool.wait_idle()
    completions = pool.get_finished()
    if len(completions) != expected_jobs:
        raise RuntimeError(
            f"seed completion 数不符：expected={expected_jobs}, got={len(completions)}"
        )
    failed = [job_id for job_id, success in completions if not success]
    if failed:
        raise RuntimeError(f"seed store 失败：job_ids={failed[:8]}")


def _seed_load_files(
    *,
    pool: Any,
    store_block: Any,
    paths_by_job: list[list[Any]],
    buffer: memoryview,
    blocks_per_job: int,
    block_size: int,
) -> None:
    # store 只读 primary buffer，因此所有 seed job 可安全复用 slot group 0。
    for job_id, paths in enumerate(paths_by_job):
        pool.enqueue_store(
            -(job_id + 1),
            blocks_per_job,
            _build_tasks(
                operation=store_block,
                paths=paths,
                buffer=buffer,
                slot_group=0,
                blocks_per_job=blocks_per_job,
                block_size=block_size,
            ),
        )
    _drain_seed_completions(pool, len(paths_by_job))


class _SlabIo:
    def __init__(self, path: Path, size: int) -> None:
        self._fd = os.open(
            path,
            os.O_CREAT | os.O_RDWR | os.O_TRUNC | os.O_CLOEXEC
            | getattr(os, "O_DIRECT", 0),
            0o644,
        )
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if hasattr(os, "posix_fallocate"):
                os.posix_fallocate(self._fd, 0, size)
            else:
                os.ftruncate(self._fd, size)
        except Exception:
            os.close(self._fd)
            self._fd = -1
            raise

    def store_block(
        self,
        secondary_offset: int,
        buffer: memoryview,
        primary_offset: int,
        block_size: int,
    ) -> None:
        view = buffer.cast("B")[primary_offset : primary_offset + block_size]
        written = os.pwritev(self._fd, [view], secondary_offset)
        if written != block_size:
            raise OSError(f"Short pwritev: expected={block_size}, got={written}")

    def load_block(
        self,
        secondary_offset: int,
        buffer: memoryview,
        primary_offset: int,
        block_size: int,
    ) -> None:
        view = buffer.cast("B")[primary_offset : primary_offset + block_size]
        read = os.preadv(self._fd, [view], secondary_offset)
        if read != block_size:
            raise OSError(f"Short preadv: expected={block_size}, got={read}")

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


def _sample_summary(samples: list[int]) -> dict[str, Any]:
    return {
        "mean": statistics.mean(samples),
        "median": statistics.median(samples),
        "p95": _percentile(samples, 0.95),
        "p99": _percentile(samples, 0.99),
        "max": max(samples),
        "samples": samples,
    }


def _run(args: argparse.Namespace) -> dict[str, Any]:
    # 延迟 import，让 --help 和静态检查不依赖目标机 vLLM 环境。
    from vllm.v1.kv_offload.tiering.fs.io import load_block, store_block
    from vllm.v1.kv_offload.tiering.fs.thread_pool import DualQueueThreadPool

    arm = "m1" if args.layout == "slab" else "m0"
    run_id = args.run_id or f"{args.direction}-{uuid.uuid4().hex[:12]}"
    run_root = args.root_dir.resolve() / f"{arm}-{run_id}"
    if run_root.exists():
        raise FileExistsError(f"run 目录已存在，拒绝复用：{run_root}")
    run_root.mkdir(parents=True)

    directions = ("load", "store") if args.direction == "mixed" else (args.direction,)
    jobs_by_direction = {direction: args.jobs for direction in directions}
    total_jobs = sum(jobs_by_direction.values())
    slot_groups = min(total_jobs, args.max_inflight_jobs)
    buffer_bytes = slot_groups * args.blocks_per_job * args.block_size_bytes
    primary_mmap = mmap.mmap(-1, buffer_bytes)
    primary_view = memoryview(primary_mmap)
    # 触碰每页并提供非零 store 数据；匿名 mmap 基址天然页对齐。逐页写一个
    # 字节，避免为大型 primary buffer 再构造一份同尺寸临时 bytes。
    for offset in range(0, buffer_bytes, mmap.PAGESIZE):
        primary_view[offset] = 0xA5

    total_secondary_slots = len(directions) * args.jobs * args.blocks_per_job
    slab_io = (
        _SlabIo(run_root / "slab.bin", total_secondary_slots * args.block_size_bytes)
        if args.layout == "slab"
        else None
    )
    if args.layout == "files":
        paths_by_direction = {
            direction: [
                [
                    _block_path(run_root / direction, job_index, block)
                    for block in range(args.blocks_per_job)
                ]
                for job_index in range(args.jobs)
            ]
            for direction in directions
        }
    else:
        paths_by_direction = {
            direction: [
                [
                    _secondary_offset(
                        (args.jobs * args.blocks_per_job
                         if args.direction == "mixed" and direction == "store"
                         else 0),
                        job_index,
                        block,
                        args.blocks_per_job,
                        args.block_size_bytes,
                    )
                    for block in range(args.blocks_per_job)
                ]
                for job_index in range(args.jobs)
            ]
            for direction in directions
        }
    pool = DualQueueThreadPool(
        args.n_read_threads,
        args.n_write_threads,
        thread_name_prefix="m0_vllm_fs",
    )

    try:
        store_operation = store_block if slab_io is None else slab_io.store_block
        load_operation = load_block if slab_io is None else slab_io.load_block
        if "load" in directions:
            _seed_load_files(
                pool=pool,
                store_block=store_operation,
                paths_by_job=paths_by_direction["load"],
                buffer=primary_view,
                blocks_per_job=args.blocks_per_job,
                block_size=args.block_size_bytes,
            )

        operations = {"load": load_operation, "store": store_operation}
        enqueues = {"load": pool.enqueue_load, "store": pool.enqueue_store}

        # 预先生成两个独立开环流的绝对时间表。相同 due time 时 load 排在 store
        # 前；正式 50:50 默认以 10ms 偏移交错，因此不会形成成对微突发。
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
                due_offset_ns = start_offset_ns + job_index * interval_ns
                schedule.append(
                    (due_offset_ns, priority, direction, job_index, next_job_id)
                )
                next_job_id += 1
        schedule.sort()

        free_groups = list(range(slot_groups - 1, -1, -1))
        job_groups: dict[int, int] = {}
        job_directions: dict[int, str] = {}
        submitted_at_ns: dict[int, int] = {}
        completion_ns: dict[int, int] = {}
        submit_call_ns: dict[str, list[int]] = {direction: [] for direction in directions}
        dispatch_lag_ns: dict[str, list[int]] = {direction: [] for direction in directions}
        failed_jobs: list[int] = []
        max_inflight_observed = 0
        max_inflight_by_direction = {direction: 0 for direction in directions}
        next_event = 0
        completed = 0
        poll_seconds = args.poll_interval_us / 1_000_000.0

        usage_before = resource.getrusage(resource.RUSAGE_SELF)
        wall_start_ns = time.perf_counter_ns()

        while completed < total_jobs:
            now_ns = time.perf_counter_ns()
            while (
                next_event < total_jobs
                and free_groups
                and now_ns >= wall_start_ns + schedule[next_event][0]
            ):
                due_offset_ns, _, direction, job_index, job_id = schedule[next_event]
                group = free_groups.pop()
                tasks = _build_tasks(
                    operation=operations[direction],
                    paths=paths_by_direction[direction][job_index],
                    buffer=primary_view,
                    slot_group=group,
                    blocks_per_job=args.blocks_per_job,
                    block_size=args.block_size_bytes,
                )
                submit_start_ns = time.perf_counter_ns()
                due_ns = wall_start_ns + due_offset_ns
                enqueues[direction](job_id, args.blocks_per_job, tasks)
                submit_end_ns = time.perf_counter_ns()
                submitted_at_ns[job_id] = submit_start_ns
                submit_call_ns[direction].append(submit_end_ns - submit_start_ns)
                dispatch_lag_ns[direction].append(max(0, submit_start_ns - due_ns))
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

            visible_completions = pool.get_finished()
            observed_ns = time.perf_counter_ns()
            for job_id, success in visible_completions:
                if job_id not in job_groups:
                    raise RuntimeError(f"未知或重复 completion：job_id={job_id}")
                completion_ns[job_id] = observed_ns
                free_groups.append(job_groups.pop(job_id))
                job_directions.pop(job_id)
                completed += 1
                if not success:
                    failed_jobs.append(job_id)

            if completed < total_jobs:
                if poll_seconds:
                    time.sleep(poll_seconds)
                else:
                    time.sleep(0)

        wall_end_ns = time.perf_counter_ns()
        usage_after = resource.getrusage(resource.RUSAGE_SELF)
    finally:
        pool.shutdown(wait=True)
        if slab_io is not None:
            slab_io.close()
        primary_view.release()
        primary_mmap.close()

    if failed_jobs:
        raise RuntimeError(f"M0 I/O job 失败：job_ids={failed_jobs[:8]}")

    scheduled_direction = {job_id: direction for _, _, direction, _, job_id in schedule}
    job_latencies_ns = {
        direction: [
            completion_ns[job_id] - submitted_at_ns[job_id]
            for job_id in range(total_jobs)
            if scheduled_direction[job_id] == direction
        ]
        for direction in directions
    }
    total_blocks = total_jobs * args.blocks_per_job
    total_bytes = total_blocks * args.block_size_bytes
    wall_seconds = (wall_end_ns - wall_start_ns) / 1_000_000_000
    direct_io = platform.system() == "Linux" and hasattr(os, "O_DIRECT")

    direction_results = {}
    for direction in directions:
        direction_jobs = jobs_by_direction[direction]
        direction_blocks = direction_jobs * args.blocks_per_job
        direction_bytes = direction_blocks * args.block_size_bytes
        direction_results[direction] = {
            "completed_jobs": direction_jobs,
            "failed_jobs": 0,
            "blocks": direction_blocks,
            "bytes": direction_bytes,
            "jobs_per_second_over_window": direction_jobs / wall_seconds,
            "blocks_per_second_over_window": direction_blocks / wall_seconds,
            "bytes_per_second_over_window": direction_bytes / wall_seconds,
            "max_inflight_jobs_observed": max_inflight_by_direction[direction],
            "job_latency_ns": _sample_summary(job_latencies_ns[direction]),
            "submit_call_ns": _sample_summary(submit_call_ns[direction]),
            "dispatch_lag_ns": _sample_summary(dispatch_lag_ns[direction]),
        }

    return {
        "schema_version": 2,
        "benchmark": "python_slab_m1" if arm == "m1" else "vllm_fs_m0",
        "run_id": run_id,
        "run_root": str(run_root),
        "valid_for_formal_comparison": direct_io,
        "configuration": {
            "direction": args.direction,
            "layout": args.layout,
            "block_size_bytes": args.block_size_bytes,
            "jobs": args.jobs,
            "blocks_per_job": args.blocks_per_job,
            "offered_qps": args.qps if args.direction != "mixed" else None,
            "read_qps": args.read_qps if args.direction == "mixed" else None,
            "write_qps": args.write_qps if args.direction == "mixed" else None,
            "write_start_offset_ms": (
                args.write_start_offset_ms if args.direction == "mixed" else None
            ),
            "n_read_threads": args.n_read_threads,
            "n_write_threads": args.n_write_threads,
            "max_inflight_jobs": args.max_inflight_jobs,
            "effective_slot_groups": slot_groups,
            "poll_interval_us": args.poll_interval_us,
            "buffer_bytes": buffer_bytes,
            "o_direct": direct_io,
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
        print(f"M0 benchmark failed: {exc}", file=sys.stderr)
        raise
