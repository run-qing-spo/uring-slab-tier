#!/usr/bin/env python3
"""按固定到达率直接发送请求，让 HTTP 在途数随服务速度自然变化。"""

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
    parser.add_argument("--prompts", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--qps", required=True, type=float)
    parser.add_argument("--max-tokens", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--phase", default="measurement")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    if args.qps <= 0:
        raise ValueError("qps 必须为正数")
    prompts = load_prompts(Path(args.prompts))
    results: list[dict[str, Any]] = []
    active = 0
    active_peak = 0
    dispatch_times: list[float] = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(args.timeout_seconds), trust_env=False
    ) as client:
        # HTTP client 初始化不属于 offered-load 时间轴；首请求应在此刻到达。
        run_start = time.perf_counter()

        async def scheduled_request(
            sequence: int, prompt: dict[str, Any]
        ) -> None:
            nonlocal active, active_peak
            scheduled_at = run_start + sequence / args.qps
            delay = scheduled_at - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            # 不等待客户端并发槽：到达时间一到便真正进入 HTTP 请求。
            dispatched_at = time.perf_counter()
            dispatch_times.append(dispatched_at)
            active += 1
            active_peak = max(active_peak, active)
            try:
                result = await send_one(
                    client,
                    args.base_url,
                    args.model,
                    prompt,
                    args.max_tokens,
                )
            finally:
                active -= 1
            result.update(
                {
                    "kind": "client_request",
                    "phase": args.phase,
                    "sequence": sequence,
                    "prompt_id": prompt["prompt_id"],
                    "expected_prompt_tokens": prompt.get("token_count"),
                    "scheduled_after_run_seconds": sequence / args.qps,
                    "dispatch_lag_seconds": dispatched_at - scheduled_at,
                }
            )
            if result["ok"] and (
                result["prompt_tokens"] != prompt.get("token_count")
                or result["completion_tokens"] != args.max_tokens
            ):
                result.update(
                    {
                        "ok": False,
                        "error_type": "TokenCountMismatch",
                        "error": "服务端 token 数与 workload 配置不一致",
                    }
                )
            results.append(result)

        await asyncio.gather(
            *(
                scheduled_request(sequence, prompt)
                for sequence, prompt in enumerate(prompts)
            )
        )

    run_end = time.perf_counter()
    results.sort(key=lambda item: item["sequence"])
    if len(results) != len(prompts) or len(
        {result["prompt_id"] for result in results}
    ) != len(prompts):
        raise RuntimeError("请求结果不满足每条 prompt 恰好一次")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            for result in results:
                stream.write(json.dumps(result, ensure_ascii=False) + "\n")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)

    failures = sum(not result["ok"] for result in results)
    duration = run_end - run_start
    dispatch_span = (
        max(dispatch_times) - min(dispatch_times)
        if len(dispatch_times) > 1
        else None
    )
    summary = {
        "kind": "open_loop_client_summary",
        "phase": args.phase,
        "target_qps": args.qps,
        "in_flight_peak": active_peak,
        "max_dispatch_lag_seconds": max(
            result["dispatch_lag_seconds"] for result in results
        ),
        "dispatch_span_seconds": dispatch_span,
        "realized_dispatch_qps": (
            (len(dispatch_times) - 1) / dispatch_span
            if dispatch_span is not None and dispatch_span > 0
            else None
        ),
        "requests": len(results),
        "failures": failures,
        "duration_seconds": duration,
        "completion_throughput_qps": len(results) / duration,
        "output": str(output),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if failures else 0


def main() -> None:
    raise SystemExit(asyncio.run(run(parse_args())))


if __name__ == "__main__":
    main()
