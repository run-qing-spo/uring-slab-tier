#!/usr/bin/env python3
"""汇总FS纯读16+16与16+0线程诊断。"""

import argparse
import glob
import json
import math
import os
import re
import statistics

from analyze_write_abba import student_t_ppf


def effect(ratios):
    logs = list(map(math.log, ratios))
    mean_log = statistics.mean(logs)
    se = statistics.stdev(logs) / math.sqrt(len(logs))
    critical = student_t_ppf(0.975, len(logs) - 1)
    return {
        "pairs": len(logs),
        "r16w0_over_r16w16": math.exp(mean_log),
        "ci95_low": math.exp(mean_log - critical * se),
        "ci95_high": math.exp(mean_log + critical * se),
        "r16w0_slower_pairs": sum(value > 1.0 for value in ratios),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    args = parser.parse_args()
    arms = []
    pattern = os.path.join(args.root, "qps-100", "*", "client-measurement.jsonl")
    for path in sorted(glob.glob(pattern)):
        name = os.path.basename(os.path.dirname(path))
        match = re.fullmatch(r"s(\d+)-p(\d+)-(r16w16|r16w0)-fs", name)
        if match is None:
            raise ValueError(name)
        records = [json.loads(line) for line in open(path, encoding="utf-8")]
        if len(records) != 128 or not all(record["ok"] for record in records):
            raise ValueError(path)
        window = json.load(open(os.path.dirname(path) + "/server-window-summary.json"))
        arms.append({
            "pair": int(match.group(2)),
            "mode": match.group(3),
            "mean_ms": statistics.mean(record["ttft_seconds"] * 1000 for record in records),
            "tail_mean_ms": statistics.mean(record["ttft_seconds"] * 1000 for record in records[32:]),
            "window": window,
        })
    pairs = []
    for number in range(1, 9):
        base = next(arm for arm in arms if arm["pair"] == number and arm["mode"] == "r16w16")
        test = next(arm for arm in arms if arm["pair"] == number and arm["mode"] == "r16w0")
        pairs.append({
            "pair": number,
            "r16w16_mean_ms": base["mean_ms"],
            "r16w0_mean_ms": test["mean_ms"],
            "ratio": test["mean_ms"] / base["mean_ms"],
            "tail_ratio": test["tail_mean_ms"] / base["tail_mean_ms"],
        })
    result = {
        "effect": effect([pair["ratio"] for pair in pairs]),
        "discard_first_32_effect": effect([pair["tail_ratio"] for pair in pairs]),
        "arithmetic_means_ms": {
            mode: statistics.mean(arm["mean_ms"] for arm in arms if arm["mode"] == mode)
            for mode in ("r16w16", "r16w0")
        },
        "validation": {
            "arms": len(arms),
            "promotion_attempts": sum(arm["window"]["primary_promotion_attempts"] for arm in arms),
            "promotion_failures": sum(arm["window"]["primary_promotion_failures"] for arm in arms),
            "store_attempts": sum(arm["window"]["primary_store_attempts"] for arm in arms),
            "preemptions": sum(arm["window"]["preemptions"] for arm in arms),
        },
        "pairs": pairs,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
