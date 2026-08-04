#!/usr/bin/env python3
"""以固定 worker 数闭环发送 prompt，并记录逐请求 TTFT。"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompts", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--concurrency", required=True, type=int)
    parser.add_argument("--max-tokens", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--phase", default="measurement")
    return parser.parse_args()


def load_prompts(path: Path) -> list[dict[str, Any]]:
    prompts: list[dict[str, Any]] = []
    prompt_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            record = json.loads(line)
            prompt_id = record["prompt_id"]
            prompt = record["prompt"]
            if not isinstance(prompt_id, str) or not isinstance(prompt, str):
                raise ValueError(f"第 {line_number} 行 prompt 字段类型错误")
            if prompt_id in prompt_ids:
                raise ValueError(f"第 {line_number} 行 prompt_id 重复")
            prompt_ids.add(prompt_id)
            prompts.append(record)
    if not prompts:
        raise ValueError("prompt 文件为空")
    return prompts


async def send_one(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    prompt_record: dict[str, Any],
    max_tokens: int,
) -> dict[str, Any]:
    request_start = time.perf_counter()
    first_token_at: float | None = None
    response_id: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    chunks = 0
    payload = {
        "model": model,
        "prompt": prompt_record["prompt"],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    try:
        async with client.stream(
            "POST", f"{base_url.rstrip('/')}/v1/completions", json=payload
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    continue
                chunk = json.loads(data)
                chunks += 1
                response_id = chunk.get("id", response_id)
                choices = chunk.get("choices") or []
                if choices:
                    text = choices[0].get("text")
                    if text is not None and first_token_at is None:
                        first_token_at = time.perf_counter()
                    finish_reason = choices[0].get("finish_reason") or finish_reason
                usage = chunk.get("usage")
                if usage:
                    prompt_tokens = usage.get("prompt_tokens")
                    completion_tokens = usage.get("completion_tokens")

        request_end = time.perf_counter()
        if first_token_at is None:
            raise RuntimeError("stream 正常结束，但没有收到输出 token")
        return {
            "ok": True,
            "response_id": response_id,
            "ttft_seconds": first_token_at - request_start,
            "latency_seconds": request_end - request_start,
            "finish_reason": finish_reason,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "stream_chunks": chunks,
        }
    except Exception as exc:
        return {
            "ok": False,
            "latency_seconds": time.perf_counter() - request_start,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


async def run(args: argparse.Namespace) -> int:
    if args.concurrency <= 0 or args.max_tokens <= 0:
        raise ValueError("concurrency 和 max-tokens 必须为正数")
    prompts = load_prompts(Path(args.prompts))
    queue: asyncio.Queue[tuple[int, dict[str, Any]]] = asyncio.Queue()
    for sequence, prompt in enumerate(prompts):
        queue.put_nowait((sequence, prompt))

    results: list[dict[str, Any]] = []
    results_lock = asyncio.Lock()
    active_requests = 0
    in_flight_peak = 0
    run_start = time.perf_counter()
    timeout = httpx.Timeout(args.timeout_seconds)

    async with httpx.AsyncClient(timeout=timeout) as client:

        async def worker(worker_id: int) -> None:
            nonlocal active_requests, in_flight_peak
            while True:
                try:
                    sequence, prompt = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                started = time.perf_counter()
                active_requests += 1
                in_flight_peak = max(in_flight_peak, active_requests)
                try:
                    result = await send_one(
                        client,
                        args.base_url,
                        args.model,
                        prompt,
                        args.max_tokens,
                    )
                finally:
                    active_requests -= 1
                result.update(
                    {
                        "kind": "client_request",
                        "phase": args.phase,
                        "sequence": sequence,
                        "worker_id": worker_id,
                        "prompt_id": prompt["prompt_id"],
                        "expected_prompt_tokens": prompt.get("token_count"),
                        "started_after_run_seconds": started - run_start,
                    }
                )
                async with results_lock:
                    results.append(result)
                queue.task_done()

        workers = [
            asyncio.create_task(worker(worker_id))
            for worker_id in range(min(args.concurrency, len(prompts)))
        ]
        await asyncio.gather(*workers)

    run_end = time.perf_counter()
    results.sort(key=lambda item: item["sequence"])
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
    summary = {
        "kind": "client_run_summary",
        "phase": args.phase,
        "concurrency": args.concurrency,
        "requests": len(results),
        "failures": failures,
        "in_flight_peak": in_flight_peak,
        "duration_seconds": run_end - run_start,
        "throughput_requests_per_second": len(results) / (run_end - run_start),
        "output": str(output),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if failures else 0


def main() -> None:
    raise SystemExit(asyncio.run(run(parse_args())))


if __name__ == "__main__":
    main()
