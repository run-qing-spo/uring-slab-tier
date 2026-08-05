#!/usr/bin/env python3
"""汇总50:50稳态混合ABBA实验，分别计算read与write效应。"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import statistics

from analyze_write_abba import student_t_ppf


def effect(pairs: list[dict[str, object]], kind: str) -> dict[str, float | int]:
    ratios = [float(pair[f"{kind}_ratio"]) for pair in pairs]
    logs = [math.log(value) for value in ratios]
    mean_log = statistics.mean(logs)
    standard_error = statistics.stdev(logs) / math.sqrt(len(logs))
    critical = student_t_ppf(0.975, len(logs) - 1)
    ratio = math.exp(mean_log)
    low = math.exp(mean_log - critical * standard_error)
    high = math.exp(mean_log + critical * standard_error)
    return {
        "pairs": len(pairs),
        "uring_over_fs": ratio,
        "ttft_reduction": 1.0 - ratio,
        "ratio_ci95_low": low,
        "ratio_ci95_high": high,
        "uring_faster_pairs": sum(value < 1.0 for value in ratios),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("roots", nargs="+")
    args = parser.parse_args()
    arms: list[dict[str, object]] = []

    for batch, root in enumerate(args.roots, 1):
        pattern = os.path.join(root, "qps-100", "*", "client-measurement-mixed-summary.json")
        for summary_path in sorted(glob.glob(pattern)):
            directory = os.path.dirname(summary_path)
            arm_dir = os.path.basename(directory)
            match = re.fullmatch(r"s(\d+)-p(\d+)-(fs|uring_slab)", arm_dir)
            if match is None:
                raise ValueError(f"无法解析arm目录：{arm_dir}")
            with open(summary_path, encoding="utf-8") as stream:
                client = json.load(stream)
            with open(os.path.join(directory, "server-window-summary.json"), encoding="utf-8") as stream:
                window = json.load(stream)
            means = {}
            for kind in ("read", "write"):
                path = os.path.join(directory, f"client-measurement-{kind}.jsonl")
                records = [json.loads(line) for line in open(path, encoding="utf-8")]
                if len(records) != 128 or not all(record["ok"] for record in records):
                    raise ValueError(f"{path}不是128条全部成功的请求")
                means[kind] = statistics.mean(record["ttft_seconds"] * 1000 for record in records)
            arms.append({
                "batch": batch,
                "sequence": int(match.group(1)),
                "pair": int(match.group(2)),
                "backend": match.group(3),
                "directory": arm_dir,
                "read_mean_ms": means["read"],
                "write_mean_ms": means["write"],
                "client": client,
                "window": window,
            })

    pairs: list[dict[str, object]] = []
    for batch in range(1, len(args.roots) + 1):
        for pair_number in sorted({int(arm["pair"]) for arm in arms if arm["batch"] == batch}):
            pair_arms = [arm for arm in arms if arm["batch"] == batch and arm["pair"] == pair_number]
            if len(pair_arms) != 2:
                raise ValueError(f"batch {batch} pair {pair_number}不完整")
            fs = next(arm for arm in pair_arms if arm["backend"] == "fs")
            uring = next(arm for arm in pair_arms if arm["backend"] == "uring_slab")
            pairs.append({
                "batch": batch,
                "pair": pair_number,
                "order": "FS-uring" if fs["sequence"] < uring["sequence"] else "uring-FS",
                "fs_read_mean_ms": fs["read_mean_ms"],
                "uring_read_mean_ms": uring["read_mean_ms"],
                "read_ratio": float(uring["read_mean_ms"]) / float(fs["read_mean_ms"]),
                "fs_write_mean_ms": fs["write_mean_ms"],
                "uring_write_mean_ms": uring["write_mean_ms"],
                "write_ratio": float(uring["write_mean_ms"]) / float(fs["write_mean_ms"]),
            })

    invalid = []
    for arm in arms:
        window, client = arm["window"], arm["client"]
        issues = []
        expected = {
            "primary_store_attempts": 128,
            "primary_store_failures": 0,
            "primary_promotion_attempts": 1024,
            "primary_promotion_failures": 0,
            "preemptions": 0,
        }
        for key, value in expected.items():
            if window[key] != value:
                issues.append(f"{key}={window[key]} expected={value}")
        for kind in ("read", "write"):
            if client[kind]["failures"] != 0:
                issues.append(f"{kind}_failures={client[kind]['failures']}")
        if issues:
            invalid.append({"directory": arm["directory"], "issues": issues})

    result = {
        "roots": args.roots,
        "read_effect": effect(pairs, "read"),
        "write_effect": effect(pairs, "write"),
        "arithmetic_arm_means_ms": {
            backend: {
                kind: statistics.mean(float(arm[f"{kind}_mean_ms"]) for arm in arms if arm["backend"] == backend)
                for kind in ("read", "write")
            }
            for backend in ("fs", "uring_slab")
        },
        "validation": {
            "arms": len(arms),
            "requests": len(arms) * 256,
            "invalid_arms": invalid,
            "store_attempts": sum(int(arm["window"]["primary_store_attempts"]) for arm in arms),
            "store_failures": sum(int(arm["window"]["primary_store_failures"]) for arm in arms),
            "promotion_attempts": sum(int(arm["window"]["primary_promotion_attempts"]) for arm in arms),
            "promotion_failures": sum(int(arm["window"]["primary_promotion_failures"]) for arm in arms),
            "preemptions": sum(int(arm["window"]["preemptions"]) for arm in arms),
            "waiting_peak_max": max(int(arm["window"]["waiting_peak"]) for arm in arms),
            "read_qps_min": min(float(arm["client"]["read"]["realized_dispatch_qps"]) for arm in arms),
            "read_qps_max": max(float(arm["client"]["read"]["realized_dispatch_qps"]) for arm in arms),
            "write_qps_min": min(float(arm["client"]["write"]["realized_dispatch_qps"]) for arm in arms),
            "write_qps_max": max(float(arm["client"]["write"]["realized_dispatch_qps"]) for arm in arms),
        },
        "pairs": pairs,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
