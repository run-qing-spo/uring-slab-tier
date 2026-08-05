#!/usr/bin/env python3
"""顺序运行 M0 的 L100/S100/L50/S50/MIX 五个 workload。"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="运行完整 FS M0 workload suite")
    parser.add_argument("--root-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--block-size-bytes", type=int, required=True)
    parser.add_argument("--jobs", type=int, default=128)
    parser.add_argument("--blocks-per-job", type=int, default=8)
    parser.add_argument("--n-read-threads", type=int, default=16)
    parser.add_argument("--n-write-threads", type=int, default=16)
    parser.add_argument("--max-inflight-jobs", type=int, default=32)
    parser.add_argument("--poll-interval-us", type=float, default=100.0)
    parser.add_argument("--keep-data", action="store_true")
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"output目录已存在，拒绝覆盖：{output_dir}")
    output_dir.mkdir(parents=True)

    benchmark = Path(__file__).with_name("benchmark_fs_m0.py")
    common = [
        sys.executable,
        str(benchmark),
        "--root-dir", str(args.root_dir.resolve()),
        "--block-size-bytes", str(args.block_size_bytes),
        "--jobs", str(args.jobs),
        "--blocks-per-job", str(args.blocks_per_job),
        "--n-read-threads", str(args.n_read_threads),
        "--n-write-threads", str(args.n_write_threads),
        "--max-inflight-jobs", str(args.max_inflight_jobs),
        "--poll-interval-us", str(args.poll_interval_us),
    ]
    workloads = [
        ("l100", ["--direction", "load", "--qps", "100"]),
        ("s100", ["--direction", "store", "--qps", "100"]),
        ("l50", ["--direction", "load", "--qps", "50"]),
        ("s50", ["--direction", "store", "--qps", "50"]),
        (
            "mix50-50",
            [
                "--direction", "mixed",
                "--read-qps", "50",
                "--write-qps", "50",
                "--write-start-offset-ms", "10",
            ],
        ),
    ]
    suite_id = uuid.uuid4().hex[:12]
    for name, workload_args in workloads:
        output = output_dir / f"m0-{name}.json"
        run_id = f"{suite_id}-{name}"
        subprocess.run(
            common + workload_args
            + ["--run-id", run_id, "--output", str(output)],
            check=True,
        )
        if not args.keep_data:
            shutil.rmtree(args.root_dir.resolve() / f"m0-{run_id}")


if __name__ == "__main__":
    main()
