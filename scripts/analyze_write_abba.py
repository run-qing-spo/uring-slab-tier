#!/usr/bin/env python3
"""汇总多批稳态纯写 ABBA 实验并输出 JSON。"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import statistics


def regularized_incomplete_beta(x: float, a: float, b: float) -> float:
    """用连分数计算正则化不完全 beta 函数。"""
    if not 0.0 <= x <= 1.0:
        raise ValueError("x必须在[0, 1]内")

    def continued_fraction(aa: float, bb: float, xx: float) -> float:
        max_iterations, epsilon, tiny = 200, 3e-14, 1e-300
        qab, qap, qam = aa + bb, aa + 1.0, aa - 1.0
        c = 1.0
        d = 1.0 - qab * xx / qap
        d = tiny if abs(d) < tiny else d
        d = 1.0 / d
        result = d
        for iteration in range(1, max_iterations + 1):
            m2 = 2 * iteration
            term = iteration * (bb - iteration) * xx / ((qam + m2) * (aa + m2))
            d = 1.0 + term * d
            d = tiny if abs(d) < tiny else d
            c = 1.0 + term / c
            c = tiny if abs(c) < tiny else c
            d = 1.0 / d
            result *= d * c
            term = -(aa + iteration) * (qab + iteration) * xx / ((aa + m2) * (qap + m2))
            d = 1.0 + term * d
            d = tiny if abs(d) < tiny else d
            c = 1.0 + term / c
            c = tiny if abs(c) < tiny else c
            d = 1.0 / d
            delta = d * c
            result *= delta
            if abs(delta - 1.0) < epsilon:
                return result
        raise RuntimeError("不完全beta连分数未收敛")

    if x in (0.0, 1.0):
        return x
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * continued_fraction(a, b, x) / a
    return 1.0 - front * continued_fraction(b, a, 1.0 - x) / b


def student_t_cdf(value: float, degrees_of_freedom: int) -> float:
    x = degrees_of_freedom / (degrees_of_freedom + value * value)
    tail = 0.5 * regularized_incomplete_beta(x, degrees_of_freedom / 2.0, 0.5)
    return 1.0 - tail if value >= 0.0 else tail


def student_t_ppf(probability: float, degrees_of_freedom: int) -> float:
    low, high = -64.0, 64.0
    for _ in range(100):
        middle = (low + high) / 2.0
        if student_t_cdf(middle, degrees_of_freedom) < probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def paired_effect(pairs: list[dict[str, object]]) -> dict[str, float | int]:
    logs = [math.log(float(pair["ratio"])) for pair in pairs]
    mean_log = statistics.mean(logs)
    standard_error = statistics.stdev(logs) / math.sqrt(len(logs))
    critical = student_t_ppf(0.975, len(logs) - 1)
    low = math.exp(mean_log - critical * standard_error)
    high = math.exp(mean_log + critical * standard_error)
    ratio = math.exp(mean_log)
    return {
        "pairs": len(pairs),
        "uring_over_fs": ratio,
        "ttft_reduction": 1.0 - ratio,
        "ratio_ci95_low": low,
        "ratio_ci95_high": high,
        "uring_faster_pairs": sum(float(pair["ratio"]) < 1.0 for pair in pairs),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("roots", nargs="+", help="ABBA批次结果根目录")
    args = parser.parse_args()

    arms: list[dict[str, object]] = []
    for batch, root in enumerate(args.roots, 1):
        pattern = os.path.join(root, "qps-100", "*", "client-measurement-write.jsonl")
        for path in sorted(glob.glob(pattern)):
            arm_dir = os.path.basename(os.path.dirname(path))
            match = re.fullmatch(r"s(\d+)-p(\d+)-(fs|uring_slab)", arm_dir)
            if match is None:
                raise ValueError(f"无法解析arm目录：{arm_dir}")
            requests = [json.loads(line) for line in open(path, encoding="utf-8")]
            if not requests or not all(item["ok"] for item in requests):
                raise ValueError(f"请求数据缺失或失败：{path}")
            directory = os.path.dirname(path)
            with open(os.path.join(directory, "server-window-summary.json"), encoding="utf-8") as stream:
                window = json.load(stream)
            with open(os.path.join(directory, "client-measurement-write-summary.json"), encoding="utf-8") as stream:
                client = json.load(stream)
            arms.append({
                "batch": batch,
                "sequence": int(match.group(1)),
                "pair": int(match.group(2)),
                "backend": match.group(3),
                "directory": arm_dir,
                "mean_ttft_ms": statistics.mean(item["ttft_seconds"] * 1000 for item in requests),
                "median_ttft_ms": statistics.median(item["ttft_seconds"] * 1000 for item in requests),
                "requests": len(requests),
                "window": window,
                "client": client,
            })

    pairs: list[dict[str, object]] = []
    for batch in range(1, len(args.roots) + 1):
        batch_arms = [arm for arm in arms if arm["batch"] == batch]
        for pair_number in sorted({int(arm["pair"]) for arm in batch_arms}):
            pair_arms = [arm for arm in batch_arms if arm["pair"] == pair_number]
            if len(pair_arms) != 2:
                raise ValueError(f"batch {batch} pair {pair_number}不完整")
            fs = next(arm for arm in pair_arms if arm["backend"] == "fs")
            uring = next(arm for arm in pair_arms if arm["backend"] == "uring_slab")
            pairs.append({
                "batch": batch,
                "pair": pair_number,
                "order": "FS-uring" if fs["sequence"] < uring["sequence"] else "uring-FS",
                "fs_mean_ttft_ms": fs["mean_ttft_ms"],
                "uring_mean_ttft_ms": uring["mean_ttft_ms"],
                "ratio": float(uring["mean_ttft_ms"]) / float(fs["mean_ttft_ms"]),
                "fs_directory": fs["directory"],
                "uring_directory": uring["directory"],
            })

    flags: list[dict[str, object]] = []
    robust_bases: dict[str, dict[str, float]] = {}
    for backend in ("fs", "uring_slab"):
        backend_arms = [arm for arm in arms if arm["backend"] == backend]
        values = [float(arm["mean_ttft_ms"]) for arm in backend_arms]
        median = statistics.median(values)
        mad = statistics.median(abs(value - median) for value in values)
        robust_bases[backend] = {"median_ms": median, "mad_ms": mad}
        for arm in backend_arms:
            robust_z = 0.6745 * (float(arm["mean_ttft_ms"]) - median) / mad if mad else 0.0
            if abs(robust_z) > 3.5:
                flags.append({
                    "batch": arm["batch"],
                    "pair": arm["pair"],
                    "backend": backend,
                    "directory": arm["directory"],
                    "mean_ttft_ms": arm["mean_ttft_ms"],
                    "robust_z": robust_z,
                })
    flagged_pairs = {(flag["batch"], flag["pair"]) for flag in flags}
    filtered_pairs = [pair for pair in pairs if (pair["batch"], pair["pair"]) not in flagged_pairs]

    result = {
        "roots": args.roots,
        "raw_effect": paired_effect(pairs),
        "arithmetic_arm_means_ms": {
            backend: statistics.mean(float(arm["mean_ttft_ms"]) for arm in arms if arm["backend"] == backend)
            for backend in ("fs", "uring_slab")
        },
        "batch_effects": {
            str(batch): paired_effect([pair for pair in pairs if pair["batch"] == batch])
            for batch in range(1, len(args.roots) + 1)
        },
        "order_effects": {
            order: paired_effect([pair for pair in pairs if pair["order"] == order])
            for order in ("FS-uring", "uring-FS")
        },
        "robust_bases": robust_bases,
        "outlier_arms": flags,
        "filtered_effect": paired_effect(filtered_pairs),
        "validation": {
            "arms": len(arms),
            "requests": sum(int(arm["requests"]) for arm in arms),
            "client_failures": sum(int(arm["client"]["failures"]) for arm in arms),
            "store_attempts": sum(int(arm["window"]["primary_store_attempts"]) for arm in arms),
            "store_failures": sum(int(arm["window"]["primary_store_failures"]) for arm in arms),
            "promotion_attempts": sum(int(arm["window"]["primary_promotion_attempts"]) for arm in arms),
            "preemptions": sum(int(arm["window"]["preemptions"]) for arm in arms),
            "waiting_peak_max": max(int(arm["window"]["waiting_peak"]) for arm in arms),
            "realized_qps_min": min(float(arm["client"]["realized_dispatch_qps"]) for arm in arms),
            "realized_qps_max": max(float(arm["client"]["realized_dispatch_qps"]) for arm in arms),
            "dispatch_lag_max_seconds": max(float(arm["client"]["max_dispatch_lag_seconds"]) for arm in arms),
        },
        "pairs": pairs,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
