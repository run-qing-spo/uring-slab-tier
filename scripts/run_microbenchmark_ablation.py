#!/usr/bin/env python3
"""运行M0→M4完整微基准消融 campaign。"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


ARMS = ("m0", "m1", "m2", "m3", "m4")
WORKLOADS: dict[str, list[str]] = {
    "l100": ["--direction", "load", "--qps", "100"],
    "s100": ["--direction", "store", "--qps", "100"],
    "l50": ["--direction", "load", "--qps", "50"],
    "s50": ["--direction", "store", "--qps", "50"],
    "mix50-50": [
        "--direction", "mixed",
        "--read-qps", "50",
        "--write-qps", "50",
        "--write-start-offset-ms", "10",
    ],
}


def _arm_order(repetition: int) -> tuple[str, ...]:
    """5次循环中每个arm恰好处于每个运行位置一次。"""
    offset = repetition % len(ARMS)
    return ARMS[offset:] + ARMS[:offset]


def _arm_command(
    arm: str,
    *,
    scripts_dir: Path,
    data_root: Path,
    block_size: int,
    jobs: int,
    blocks_per_job: int,
    pending_capacity: int,
    max_inflight_jobs: int,
    poll_interval_us: float,
) -> tuple[list[str], str]:
    common = [
        "--root-dir", str(data_root),
        "--block-size-bytes", str(block_size),
        "--jobs", str(jobs),
        "--blocks-per-job", str(blocks_per_job),
        "--max-inflight-jobs", str(max_inflight_jobs),
        "--poll-interval-us", str(poll_interval_us),
    ]
    if arm in {"m0", "m1"}:
        return (
            [
                sys.executable,
                str(scripts_dir / "benchmark_fs_m0.py"),
                "--layout", "files" if arm == "m0" else "slab",
                "--n-read-threads", "16",
                "--n-write-threads", "16",
                *common,
            ],
            arm,
        )
    engine_args = [
        sys.executable,
        str(scripts_dir / "benchmark_uring_m4.py"),
        "--pending-capacity", str(pending_capacity),
        *common,
    ]
    if arm == "m2":
        engine_args += ["--engine", "blocking", "--workers", "32"]
    elif arm == "m3":
        engine_args += [
            "--engine", "uring",
            "--total-qd", "32",
            "--submit-batch-size", "1",
        ]
    else:
        engine_args += [
            "--engine", "uring",
            "--total-qd", "32",
            "--submit-batch-size", "0",
        ]
    return engine_args, arm


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="运行M0-M4微基准消融campaign")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--block-size-bytes", type=int, required=True)
    parser.add_argument("--jobs", type=int, default=128)
    parser.add_argument("--blocks-per-job", type=int, default=8)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--pending-capacity", type=int, default=4096)
    parser.add_argument("--max-inflight-jobs", type=int, default=32)
    parser.add_argument("--poll-interval-us", type=float, default=100.0)
    parser.add_argument("--keep-data", action="store_true")
    args = parser.parse_args()

    if args.repetitions < 1:
        raise ValueError("--repetitions必须大于0")
    output_dir = args.output_dir.resolve()
    data_root = args.data_root.resolve()
    if output_dir.exists():
        raise FileExistsError(f"output目录已存在，拒绝覆盖：{output_dir}")
    output_dir.mkdir(parents=True)
    data_root.mkdir(parents=True, exist_ok=True)

    scripts_dir = Path(__file__).resolve().parent
    campaign_id = uuid.uuid4().hex[:12]
    configuration = {
        "campaign_id": campaign_id,
        "arms": list(ARMS),
        "workloads": list(WORKLOADS),
        "repetitions": args.repetitions,
        "block_size_bytes": args.block_size_bytes,
        "jobs_per_direction": args.jobs,
        "blocks_per_job": args.blocks_per_job,
        "concurrency_anchor": 32,
        "m0_m1_threads": {"read": 16, "write": 16},
        "m2_workers": 32,
        "m3_m4_total_qd": 32,
        "pending_capacity": args.pending_capacity,
        "max_inflight_jobs": args.max_inflight_jobs,
        "poll_interval_us": args.poll_interval_us,
        "keep_data": args.keep_data,
    }
    _write_json(output_dir / "configuration.json", configuration)
    manifest_path = output_dir / "manifest.jsonl"

    with manifest_path.open("w", encoding="utf-8") as manifest:
        for workload, workload_args in WORKLOADS.items():
            for repetition in range(args.repetitions):
                order = _arm_order(repetition)
                for position, arm in enumerate(order):
                    run_id = (
                        f"{campaign_id}-{workload}-r{repetition + 1:02d}-"
                        f"p{position + 1}-{arm}"
                    )
                    result_dir = output_dir / workload / f"r{repetition + 1:02d}"
                    result_dir.mkdir(parents=True, exist_ok=True)
                    result_path = result_dir / f"p{position + 1}-{arm}.json"
                    log_path = result_dir / f"p{position + 1}-{arm}.log"
                    command, data_prefix = _arm_command(
                        arm,
                        scripts_dir=scripts_dir,
                        data_root=data_root,
                        block_size=args.block_size_bytes,
                        jobs=args.jobs,
                        blocks_per_job=args.blocks_per_job,
                        pending_capacity=args.pending_capacity,
                        max_inflight_jobs=args.max_inflight_jobs,
                        poll_interval_us=args.poll_interval_us,
                    )
                    command += [
                        *workload_args,
                        "--run-id", run_id,
                        "--output", str(result_path),
                    ]
                    started_ns = time.time_ns()
                    record = {
                        "campaign_id": campaign_id,
                        "workload": workload,
                        "repetition": repetition + 1,
                        "position": position + 1,
                        "order": list(order),
                        "arm": arm,
                        "run_id": run_id,
                        "result": str(result_path),
                        "log": str(log_path),
                        "started_ns": started_ns,
                    }
                    with log_path.open("w", encoding="utf-8") as log:
                        completed = subprocess.run(
                            command,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            text=True,
                            check=False,
                        )
                    record["finished_ns"] = time.time_ns()
                    record["exit_code"] = completed.returncode
                    manifest.write(json.dumps(record, ensure_ascii=False) + "\n")
                    manifest.flush()
                    if completed.returncode != 0:
                        raise RuntimeError(
                            f"{workload} repetition={repetition + 1} {arm}失败，"
                            f"查看{log_path}"
                        )
                    if not args.keep_data:
                        shutil.rmtree(data_root / f"{data_prefix}-{run_id}")


if __name__ == "__main__":
    main()
