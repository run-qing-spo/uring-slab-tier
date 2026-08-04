#!/usr/bin/env python3
"""汇总 none sweep，并检查目标工作点是否已有原生瓶颈。"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True)
    parser.add_argument("--concurrencies", nargs="+", type=int, required=True)
    parser.add_argument("--prompt-tokens", type=int, required=True)
    parser.add_argument("--block-size", type=int, required=True)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{path}:{line_number} 不是有效 JSON") from exc
    return records


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def latency_summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "p50_seconds": percentile(values, 0.50),
        "p95_seconds": percentile(values, 0.95),
        "p99_seconds": percentile(values, 0.99),
        "max_seconds": max(values) if values else None,
    }


def matching_record(
    records: list[dict[str, Any]], kind: str, response_id: str
) -> dict[str, Any] | None:
    matches = [
        record
        for record in records
        if record.get("kind") == kind
        and str(record.get("request_id", "")).startswith(f"{response_id}-")
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def qualify_point(
    root: Path, concurrency: int, expected_blocks: int
) -> dict[str, Any]:
    point = root / f"concurrency-{concurrency}"
    issues: list[str] = []
    client_path = point / "client-measurement.jsonl"
    summary_path = point / "server-window-summary.json"
    if not client_path.is_file() or not summary_path.is_file():
        return {
            "concurrency": concurrency,
            "passed": False,
            "issues": ["缺少客户端结果或 server 窗口汇总"],
        }

    clients = read_jsonl(client_path)
    window = read_json(summary_path)
    monitor_records: list[dict[str, Any]] = []
    for path in sorted(point.glob("tiering-monitor.*.jsonl")):
        monitor_records.extend(read_jsonl(path))

    failed_clients = [record for record in clients if not record.get("ok")]
    if failed_clients:
        issues.append(f"客户端失败请求数={len(failed_clients)}")
    if len({record.get("prompt_id") for record in clients}) != len(clients):
        issues.append("客户端结果存在重复 prompt_id")

    server_ttfts: list[float] = []
    client_ttfts: list[float] = []
    missing_ttft = 0
    invalid_store = 0
    for client in clients:
        if client.get("ttft_seconds") is not None:
            client_ttfts.append(float(client["ttft_seconds"]))
        response_id = client.get("response_id")
        if not response_id:
            missing_ttft += 1
            invalid_store += 1
            continue

        ttft = matching_record(monitor_records, "ttft", response_id)
        if ttft is None:
            missing_ttft += 1
        else:
            server_ttfts.append(float(ttft["seconds"]))

        tiering = matching_record(monitor_records, "tiering_request", response_id)
        request = tiering.get("request", {}) if tiering else {}
        prepare = request.get("primary_prepare_store", {})
        transfer = request.get("gpu_to_cpu", {})
        if not (
            prepare.get("occurrences") == 1
            and prepare.get("successes") == 1
            and prepare.get("requested_blocks") == expected_blocks
            and prepare.get("reserved_blocks") == expected_blocks
            and transfer.get("occurrences") == 1
            and transfer.get("blocks") == expected_blocks
        ):
            invalid_store += 1

    if missing_ttft:
        issues.append(f"缺少或重复的 server TTFT 数={missing_ttft}")
    if invalid_store:
        issues.append(f"未完成完整 {expected_blocks}-block store 的请求数={invalid_store}")
    if window.get("waiting_peak") != 0:
        issues.append(f"waiting_peak={window.get('waiting_peak')}，期望为 0")
    if window.get("preemptions") != 0:
        issues.append(f"preemptions={window.get('preemptions')}，期望为 0")
    if window.get("primary_store_failures") != 0:
        issues.append(
            f"primary_store_failures={window.get('primary_store_failures')}，期望为 0"
        )
    if window.get("primary_store_attempts") != len(clients):
        issues.append(
            "primary_store_attempts 与正式请求数不一致："
            f"{window.get('primary_store_attempts')}/{len(clients)}"
        )
    if window.get("primary_capacity_changed"):
        issues.append("正式窗口内 primary capacity 发生变化")
    free_min = window.get("primary_free_blocks_min")
    if free_min is None or free_min <= 0:
        issues.append(f"primary_free_blocks_min={free_min}，primary 已耗尽或未观测")
    if window.get("primary_promotion_attempts") != 0:
        issues.append("纯写窗口出现了 primary promotion")

    return {
        "concurrency": concurrency,
        "passed": not issues,
        "issues": issues,
        "requests": len(clients),
        "expected_store_blocks_per_request": expected_blocks,
        "server_ttft": latency_summary(server_ttfts),
        "client_ttft_diagnostic": latency_summary(client_ttfts),
        "server_window": window,
    }


def main() -> None:
    args = parse_args()
    root = Path(args.results)
    if args.prompt_tokens % args.block_size:
        raise ValueError("prompt-tokens 必须能被 block-size 整除")
    expected_blocks = args.prompt_tokens // args.block_size
    points = [
        qualify_point(root, concurrency, expected_blocks)
        for concurrency in args.concurrencies
    ]
    result = {
        "kind": "none_qualification",
        "passed": all(point["passed"] for point in points),
        "scope": "仅判断这些工作点的原生底座是否已出现瓶颈，不与 secondary tier 比性能",
        "points": points,
    }
    output = root / "qualification.json"
    with output.open("w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")

    report = root / "qualification.txt"
    with report.open("w", encoding="utf-8") as stream:
        stream.write(f"none qualification: {'PASS' if result['passed'] else 'FAIL'}\n")
        for point in points:
            ttft = point.get("server_ttft", {})
            stream.write(
                f"c={point['concurrency']}: "
                f"{'PASS' if point['passed'] else 'FAIL'}, "
                f"server_ttft_p50={ttft.get('p50_seconds')}, "
                f"p95={ttft.get('p95_seconds')}, p99={ttft.get('p99_seconds')}\n"
            )
            for issue in point["issues"]:
                stream.write(f"  - {issue}\n")
    print(report.read_text(encoding="utf-8"), end="")


if __name__ == "__main__":
    main()
