#!/usr/bin/env python3
"""汇总纯写 ABBA 的逐请求 server TTFT 四段账本。"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import statistics
from typing import Any


STAGES = (
    "frontend_to_core_queue_seconds",
    "core_queue_seconds",
    "core_scheduled_to_output_seconds",
    "core_output_to_frontend_seconds",
)
DIAGNOSTICS = ("queue_seconds", "prefill_seconds")


def _mean(records: list[dict[str, Any]], field: str) -> float:
    return statistics.mean(float(record[field]) for record in records)


def _paired_summary(
    pairs: list[dict[str, Any]], field: str, baseline: str, treatment: str
) -> dict[str, float | int]:
    deltas = [
        float(pair[baseline][field]) - float(pair[treatment][field])
        for pair in pairs
    ]
    return {
        "pairs": len(deltas),
        "baseline_minus_treatment_mean_seconds": statistics.mean(deltas),
        "baseline_minus_treatment_median_seconds": statistics.median(deltas),
        "baseline_slower_pairs": sum(delta > 0.0 for delta in deltas),
    }


def _geometric_ratio(
    pairs: list[dict[str, Any]], baseline: str, treatment: str
) -> float:
    logs = [
        math.log(
            float(pair[treatment]["server_ttft_seconds"])
            / float(pair[baseline]["server_ttft_seconds"])
        )
        for pair in pairs
    ]
    return math.exp(statistics.mean(logs))


def _load_ttft_records(arm_dir: str, measurement_requests: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in glob.glob(os.path.join(arm_dir, "tiering-monitor*.jsonl")):
        current = [
            record
            for record in (
                json.loads(line) for line in open(path, encoding="utf-8")
            )
            if record.get("kind") == "ttft"
        ]
        if current:
            if records:
                raise ValueError(f"多个monitor文件包含TTFT：{arm_dir}")
            records = current
    if len(records) < measurement_requests:
        raise ValueError(
            f"TTFT记录不足：{arm_dir}, {len(records)} < {measurement_requests}"
        )
    # 纯写runner严格按warmup、conditioning、measurement顺序运行。
    return records[-measurement_requests:]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", help="一次16-arm纯写ABBA结果根目录")
    parser.add_argument("--qps", type=int, default=100)
    parser.add_argument("--measurement-requests", type=int, default=128)
    parser.add_argument("--mad-z-threshold", type=float, default=3.5)
    parser.add_argument("--baseline", default="fs")
    parser.add_argument("--treatment", default="uring_slab")
    args = parser.parse_args()
    expected_backends = {args.baseline, args.treatment}
    if len(expected_backends) != 2:
        raise ValueError("baseline和treatment必须不同")

    arm_pattern = os.path.join(args.root, f"qps-{args.qps}", "*")
    arms: list[dict[str, Any]] = []
    accounting_errors: list[float] = []
    for arm_dir in sorted(glob.glob(arm_pattern)):
        arm_name = os.path.basename(arm_dir)
        match = re.fullmatch(r"s(\d+)-p(\d+)-(.+)", arm_name)
        if match is None:
            continue
        if match.group(3) not in expected_backends:
            continue
        records = _load_ttft_records(arm_dir, args.measurement_requests)
        required = {"seconds", "accounting_error_seconds", *STAGES, *DIAGNOSTICS}
        for record in records:
            missing = required - record.keys()
            if missing:
                raise ValueError(f"{arm_name}缺少TTFT字段：{sorted(missing)}")
            accounting_errors.append(abs(float(record["accounting_error_seconds"])))

        arm: dict[str, Any] = {
            "sequence": int(match.group(1)),
            "pair": int(match.group(2)),
            "backend": match.group(3),
            "directory": arm_name,
            "requests": len(records),
            "server_ttft_seconds": _mean(records, "seconds"),
            "accounting_error_abs_max_seconds": max(
                abs(float(record["accounting_error_seconds"]))
                for record in records
            ),
        }
        for field in (*STAGES, *DIAGNOSTICS):
            arm[field] = _mean(records, field)
        arms.append(arm)

    if len(arms) != 16:
        raise ValueError(f"需要16个arm，实际为{len(arms)}")

    pairs: list[dict[str, Any]] = []
    for pair_number in range(1, 9):
        pair_arms = [arm for arm in arms if arm["pair"] == pair_number]
        if len(pair_arms) != 2:
            raise ValueError(f"pair {pair_number}不完整")
        baseline = next(
            arm for arm in pair_arms if arm["backend"] == args.baseline
        )
        treatment = next(
            arm for arm in pair_arms if arm["backend"] == args.treatment
        )
        pairs.append(
            {
                "pair": pair_number,
                "order": (
                    f"{args.baseline}-{args.treatment}"
                    if int(baseline["sequence"]) < int(treatment["sequence"])
                    else f"{args.treatment}-{args.baseline}"
                ),
                args.baseline: baseline,
                args.treatment: treatment,
            }
        )

    outliers: list[dict[str, Any]] = []
    robust_bases: dict[str, dict[str, float]] = {}
    for backend in (args.baseline, args.treatment):
        backend_arms = [arm for arm in arms if arm["backend"] == backend]
        values = [float(arm["server_ttft_seconds"]) for arm in backend_arms]
        median = statistics.median(values)
        mad = statistics.median(abs(value - median) for value in values)
        robust_bases[backend] = {"median_seconds": median, "mad_seconds": mad}
        for arm in backend_arms:
            robust_z = (
                0.6745 * (float(arm["server_ttft_seconds"]) - median) / mad
                if mad
                else 0.0
            )
            if abs(robust_z) > args.mad_z_threshold:
                outliers.append(
                    {
                        "pair": arm["pair"],
                        "backend": backend,
                        "directory": arm["directory"],
                        "server_ttft_seconds": arm["server_ttft_seconds"],
                        "robust_z": robust_z,
                    }
                )

    flagged_pairs = {int(item["pair"]) for item in outliers}
    filtered_pairs = [pair for pair in pairs if pair["pair"] not in flagged_pairs]
    if not filtered_pairs:
        raise ValueError("离群过滤后没有剩余配对")
    fields = ("server_ttft_seconds", *STAGES, *DIAGNOSTICS)

    result = {
        "root": args.root,
        "configuration": {
            "qps": args.qps,
            "measurement_requests": args.measurement_requests,
            "mad_z_threshold": args.mad_z_threshold,
            "baseline": args.baseline,
            "treatment": args.treatment,
        },
        "validation": {
            "arms": len(arms),
            "requests": sum(int(arm["requests"]) for arm in arms),
            "accounting_error_abs_max_seconds": max(accounting_errors),
        },
        "robust_bases": robust_bases,
        "outlier_arms": outliers,
        "raw": {
            "server_ttft_treatment_over_baseline": _geometric_ratio(
                pairs, args.baseline, args.treatment
            ),
            "fields": {
                field: _paired_summary(
                    pairs, field, args.baseline, args.treatment
                )
                for field in fields
            },
        },
        "filtered": {
            "pairs": len(filtered_pairs),
            "server_ttft_treatment_over_baseline": _geometric_ratio(
                filtered_pairs, args.baseline, args.treatment
            ),
            "fields": {
                field: _paired_summary(
                    filtered_pairs, field, args.baseline, args.treatment
                )
                for field in fields
            },
        },
        "pairs": pairs,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
