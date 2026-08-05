#!/usr/bin/env python3
"""以两个独立开环发送端产生稳态读写混合负载。"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx

from run_closed_loop_client import load_prompts, send_one


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--read-prompts", required=True)
    parser.add_argument("--write-prompts", required=True)
    parser.add_argument("--read-output", required=True)
    parser.add_argument("--write-output", required=True)
    parser.add_argument("--read-qps", required=True, type=float)
    parser.add_argument("--write-qps", required=True, type=float)
    parser.add_argument("--write-offset-seconds", type=float, default=0.01)
    parser.add_argument("--max-tokens", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    return parser.parse_args()


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def run(args: argparse.Namespace) -> int:
    if args.read_qps <= 0 or args.write_qps <= 0:
        raise ValueError("read-qps和write-qps必须为正数")
    if args.write_offset_seconds < 0:
        raise ValueError("write-offset-seconds不能为负数")

    streams = {
        "read": {
            "prompts": load_prompts(Path(args.read_prompts)),
            "qps": args.read_qps,
            "offset": 0.0,
            "output": Path(args.read_output),
        },
        "write": {
            "prompts": load_prompts(Path(args.write_prompts)),
            "qps": args.write_qps,
            "offset": args.write_offset_seconds,
            "output": Path(args.write_output),
        },
    }
    read_ids = {item["prompt_id"] for item in streams["read"]["prompts"]}
    write_ids = {item["prompt_id"] for item in streams["write"]["prompts"]}
    if read_ids & write_ids:
        raise ValueError("正式read和write prompt集合必须互不相交")

    results: dict[str, list[dict[str, Any]]] = {"read": [], "write": []}
    dispatch_times: dict[str, list[float]] = {"read": [], "write": []}
    active: dict[str, int] = {"read": 0, "write": 0}
    active_peak: dict[str, int] = {"read": 0, "write": 0}
    total_active = 0
    total_active_peak = 0
    timeout = httpx.Timeout(args.timeout_seconds)

    async with (
        httpx.AsyncClient(timeout=timeout, trust_env=False) as read_client,
        httpx.AsyncClient(timeout=timeout, trust_env=False) as write_client,
    ):
        # 给两个client相同的未来起点，确保连接初始化时间不进入发送时间线。
        run_start = time.perf_counter() + 0.05

        async def scheduled_request(
            stream_name: str,
            client: httpx.AsyncClient,
            sequence: int,
            prompt: dict[str, Any],
        ) -> None:
            nonlocal total_active, total_active_peak
            stream = streams[stream_name]
            scheduled_after = float(stream["offset"]) + sequence / float(stream["qps"])
            scheduled_at = run_start + scheduled_after
            delay = scheduled_at - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            dispatched_at = time.perf_counter()
            dispatch_times[stream_name].append(dispatched_at)
            active[stream_name] += 1
            active_peak[stream_name] = max(active_peak[stream_name], active[stream_name])
            total_active += 1
            total_active_peak = max(total_active_peak, total_active)
            try:
                result = await send_one(
                    client, args.base_url, args.model, prompt, args.max_tokens
                )
            finally:
                active[stream_name] -= 1
                total_active -= 1
            result.update({
                "kind": "client_request",
                "phase": f"measurement-{stream_name}",
                "sender": stream_name,
                "sequence": sequence,
                "prompt_id": prompt["prompt_id"],
                "expected_prompt_tokens": prompt.get("token_count"),
                "scheduled_after_run_seconds": scheduled_after,
                "dispatch_lag_seconds": dispatched_at - scheduled_at,
            })
            if result["ok"] and (
                result["prompt_tokens"] != prompt.get("token_count")
                or result["completion_tokens"] != args.max_tokens
            ):
                result.update({
                    "ok": False,
                    "error_type": "TokenCountMismatch",
                    "error": "服务端 token 数与 workload 配置不一致",
                })
            results[stream_name].append(result)

        tasks = []
        for stream_name, client in (("read", read_client), ("write", write_client)):
            tasks.extend(
                scheduled_request(stream_name, client, sequence, prompt)
                for sequence, prompt in enumerate(streams[stream_name]["prompts"])
            )
        await asyncio.gather(*tasks)
        run_end = time.perf_counter()

    summaries: dict[str, dict[str, Any]] = {}
    failures = 0
    for stream_name, stream in streams.items():
        stream_results = results[stream_name]
        stream_results.sort(key=lambda item: item["sequence"])
        prompts = stream["prompts"]
        if len(stream_results) != len(prompts) or len(
            {item["prompt_id"] for item in stream_results}
        ) != len(prompts):
            raise RuntimeError(f"{stream_name}请求未满足每条prompt恰好一次")
        write_jsonl(stream["output"], stream_results)
        stream_failures = sum(not item["ok"] for item in stream_results)
        failures += stream_failures
        times = dispatch_times[stream_name]
        span = max(times) - min(times) if len(times) > 1 else None
        summaries[stream_name] = {
            "target_qps": stream["qps"],
            "requests": len(stream_results),
            "failures": stream_failures,
            "in_flight_peak": active_peak[stream_name],
            "max_dispatch_lag_seconds": max(
                item["dispatch_lag_seconds"] for item in stream_results
            ),
            "dispatch_span_seconds": span,
            "realized_dispatch_qps": (
                (len(times) - 1) / span if span is not None and span > 0 else None
            ),
            "output": str(stream["output"]),
        }

    print(json.dumps({
        "kind": "mixed_open_loop_client_summary",
        "duration_seconds": run_end - run_start,
        "total_target_qps": args.read_qps + args.write_qps,
        "total_in_flight_peak": total_active_peak,
        "write_offset_seconds": args.write_offset_seconds,
        "read": summaries["read"],
        "write": summaries["write"],
    }, ensure_ascii=False))
    return 1 if failures else 0


def main() -> None:
    raise SystemExit(asyncio.run(run(parse_args())))


if __name__ == "__main__":
    main()
