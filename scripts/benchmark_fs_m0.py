#!/usr/bin/env python3
"""M0 微基准：复用 vLLM 原生 FS 数据面测试纯 load/store。

本脚本不构造 tier control plane，也不执行 lookup。它直接复用：

* vllm.v1.kv_offload.tiering.fs.io.store_block/load_block
* vllm.v1.kv_offload.tiering.fs.thread_pool.DualQueueThreadPool

因此保留原生 FS 的 per-block 文件、O_DIRECT、open/read/write/close、store
临时文件加 rename、双队列线程池和 job completion 聚合语义。
"""

from __future__ import annotations

import argparse
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
        description="复用 vLLM 原生 FS 数据面的 M0 纯 load/store 微基准"
    )
    parser.add_argument("--direction", choices=("load", "store"), required=True)
    parser.add_argument("--root-dir", type=Path, required=True)
    parser.add_argument("--block-size-bytes", type=int, required=True)
    parser.add_argument("--jobs", type=int, default=128)
    parser.add_argument("--blocks-per-job", type=int, default=8)
    parser.add_argument("--qps", type=float, default=100.0,
                        help="job 提交速率；0 表示尽快提交")
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
        "n-read-threads": args.n_read_threads,
        "n-write-threads": args.n_write_threads,
        "max-inflight-jobs": args.max_inflight_jobs,
    }
    for name, value in positive.items():
        if value <= 0:
            raise ValueError(f"--{name} 必须大于 0，得到 {value}")
    if args.qps < 0:
        raise ValueError(f"--qps 不能小于 0，得到 {args.qps}")
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
    paths: list[str],
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
    paths_by_job: list[list[str]],
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


def _run(args: argparse.Namespace) -> dict[str, Any]:
    # 延迟 import，让 --help 和静态检查不依赖目标机 vLLM 环境。
    from vllm.v1.kv_offload.tiering.fs.io import load_block, store_block
    from vllm.v1.kv_offload.tiering.fs.thread_pool import DualQueueThreadPool

    run_id = args.run_id or f"{args.direction}-{uuid.uuid4().hex[:12]}"
    run_root = args.root_dir.resolve() / f"m0-{run_id}"
    if run_root.exists():
        raise FileExistsError(f"run 目录已存在，拒绝复用：{run_root}")
    run_root.mkdir(parents=True)

    slot_groups = min(args.jobs, args.max_inflight_jobs)
    buffer_bytes = slot_groups * args.blocks_per_job * args.block_size_bytes
    primary_mmap = mmap.mmap(-1, buffer_bytes)
    primary_view = memoryview(primary_mmap)
    # 触碰每页并提供非零 store 数据；匿名 mmap 基址天然页对齐。逐页写一个
    # 字节，避免为大型 primary buffer 再构造一份同尺寸临时 bytes。
    for offset in range(0, buffer_bytes, mmap.PAGESIZE):
        primary_view[offset] = 0xA5

    paths_by_job = [
        [_block_path(run_root, job_id, block) for block in range(args.blocks_per_job)]
        for job_id in range(args.jobs)
    ]
    pool = DualQueueThreadPool(
        args.n_read_threads,
        args.n_write_threads,
        thread_name_prefix="m0_vllm_fs",
    )

    try:
        if args.direction == "load":
            _seed_load_files(
                pool=pool,
                store_block=store_block,
                paths_by_job=paths_by_job,
                buffer=primary_view,
                blocks_per_job=args.blocks_per_job,
                block_size=args.block_size_bytes,
            )

        operation = load_block if args.direction == "load" else store_block
        enqueue = pool.enqueue_load if args.direction == "load" else pool.enqueue_store
        free_groups = list(range(slot_groups - 1, -1, -1))
        job_groups: dict[int, int] = {}
        submitted_at_ns: dict[int, int] = {}
        completion_ns: dict[int, int] = {}
        submit_call_ns: list[int] = []
        dispatch_lag_ns: list[int] = []
        failed_jobs: list[int] = []
        max_inflight_observed = 0
        next_job = 0
        completed = 0
        poll_seconds = args.poll_interval_us / 1_000_000.0
        interval_ns = 0 if args.qps == 0 else round(1_000_000_000 / args.qps)

        usage_before = resource.getrusage(resource.RUSAGE_SELF)
        wall_start_ns = time.perf_counter_ns()
        next_due_ns = wall_start_ns

        while completed < args.jobs:
            now_ns = time.perf_counter_ns()
            while (
                next_job < args.jobs
                and free_groups
                and (interval_ns == 0 or now_ns >= next_due_ns)
            ):
                group = free_groups.pop()
                job_id = next_job
                tasks = _build_tasks(
                    operation=operation,
                    paths=paths_by_job[job_id],
                    buffer=primary_view,
                    slot_group=group,
                    blocks_per_job=args.blocks_per_job,
                    block_size=args.block_size_bytes,
                )
                submit_start_ns = time.perf_counter_ns()
                due_ns = next_due_ns if interval_ns else submit_start_ns
                enqueue(job_id, args.blocks_per_job, tasks)
                submit_end_ns = time.perf_counter_ns()
                submitted_at_ns[job_id] = submit_start_ns
                submit_call_ns.append(submit_end_ns - submit_start_ns)
                dispatch_lag_ns.append(max(0, submit_start_ns - due_ns))
                job_groups[job_id] = group
                next_job += 1
                if interval_ns:
                    next_due_ns = wall_start_ns + next_job * interval_ns
                max_inflight_observed = max(max_inflight_observed, len(job_groups))
                now_ns = submit_end_ns

            visible_completions = pool.get_finished()
            observed_ns = time.perf_counter_ns()
            for job_id, success in visible_completions:
                if job_id not in job_groups:
                    raise RuntimeError(f"未知或重复 completion：job_id={job_id}")
                completion_ns[job_id] = observed_ns
                free_groups.append(job_groups.pop(job_id))
                completed += 1
                if not success:
                    failed_jobs.append(job_id)

            if completed < args.jobs:
                if poll_seconds:
                    time.sleep(poll_seconds)
                else:
                    time.sleep(0)

        wall_end_ns = time.perf_counter_ns()
        usage_after = resource.getrusage(resource.RUSAGE_SELF)
    finally:
        pool.shutdown(wait=True)
        primary_view.release()
        primary_mmap.close()

    if failed_jobs:
        raise RuntimeError(f"M0 I/O job 失败：job_ids={failed_jobs[:8]}")

    job_latencies_ns = [completion_ns[i] - submitted_at_ns[i] for i in range(args.jobs)]
    total_blocks = args.jobs * args.blocks_per_job
    total_bytes = total_blocks * args.block_size_bytes
    wall_seconds = (wall_end_ns - wall_start_ns) / 1_000_000_000
    direct_io = platform.system() == "Linux" and hasattr(os, "O_DIRECT")

    return {
        "schema_version": 1,
        "benchmark": "vllm_fs_m0",
        "run_id": run_id,
        "run_root": str(run_root),
        "valid_for_formal_comparison": direct_io,
        "configuration": {
            "direction": args.direction,
            "block_size_bytes": args.block_size_bytes,
            "jobs": args.jobs,
            "blocks_per_job": args.blocks_per_job,
            "offered_qps": args.qps,
            "n_read_threads": args.n_read_threads,
            "n_write_threads": args.n_write_threads,
            "max_inflight_jobs": args.max_inflight_jobs,
            "effective_slot_groups": slot_groups,
            "poll_interval_us": args.poll_interval_us,
            "buffer_bytes": buffer_bytes,
            "o_direct": direct_io,
        },
        "result": {
            "completed_jobs": args.jobs,
            "failed_jobs": 0,
            "blocks": total_blocks,
            "bytes": total_bytes,
            "wall_seconds": wall_seconds,
            "jobs_per_second": args.jobs / wall_seconds,
            "blocks_per_second": total_blocks / wall_seconds,
            "bytes_per_second": total_bytes / wall_seconds,
            "max_inflight_jobs_observed": max_inflight_observed,
            "job_latency_ns": {
                "mean": statistics.mean(job_latencies_ns),
                "median": statistics.median(job_latencies_ns),
                "p95": _percentile(job_latencies_ns, 0.95),
                "p99": _percentile(job_latencies_ns, 0.99),
                "max": max(job_latencies_ns),
                "samples": job_latencies_ns,
            },
            "submit_call_ns": {
                "mean": statistics.mean(submit_call_ns),
                "median": statistics.median(submit_call_ns),
                "p95": _percentile(submit_call_ns, 0.95),
                "p99": _percentile(submit_call_ns, 0.99),
                "max": max(submit_call_ns),
                "samples": submit_call_ns,
            },
            "dispatch_lag_ns": {
                "mean": statistics.mean(dispatch_lag_ns),
                "p95": _percentile(dispatch_lag_ns, 0.95),
                "max": max(dispatch_lag_ns),
                "samples": dispatch_lag_ns,
            },
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
