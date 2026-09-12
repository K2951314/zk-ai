"""Benchmark an alias / model through the gateway API.

Usage::

    # start the gateway first
    uv run uvicorn app.main:app --port 8317

    uv run python scripts/benchmark.py --model zk-coding --runs 5
    uv run python scripts/benchmark.py --model zk-fast --prompt "写一个快排" --runs 3

Reports latency percentiles, token throughput and which provider/credential
served each run (read from the ``x-zkai-*`` response headers).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from typing import Any

import httpx

PROMPTS = [
    "Write a Python function that merges two sorted lists.",
    "Explain the difference between a process and a thread in two sentences.",
    "Return a JSON object with keys a and b set to 1 and 2.",
]


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(ratio * (len(ordered) - 1))))
    return ordered[index]


async def run_once(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    stream: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if stream:
        chunks = 0
        text = ""
        async with client.stream(
            "POST", f"{base_url}/v1/chat/completions", json=payload
        ) as response:
            headers = dict(response.headers)
            status = response.status_code
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunks += 1
                try:
                    parsed = json.loads(data)
                    text += (
                        (parsed.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
                    )
                except json.JSONDecodeError:
                    continue
        usage = {"prompt_tokens": 0, "completion_tokens": len(text) // 4}
    else:
        response = await client.post(f"{base_url}/v1/chat/completions", json=payload)
        headers = dict(response.headers)
        status = response.status_code
        body = response.json()
        chunks = 1
        usage = body.get("usage") or {}
        text = (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""

    return {
        "status": status,
        "latency_ms": (time.perf_counter() - started) * 1000,
        "provider": headers.get("x-zkai-provider"),
        "credential": headers.get("x-zkai-credential"),
        "model": headers.get("x-zkai-model"),
        "attempt": headers.get("x-zkai-attempt"),
        "fallback": headers.get("x-zkai-fallback"),
        "chunks": chunks,
        "chars": len(text),
        "usage": usage,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark ZK-AI through its API")
    parser.add_argument("--base-url", default="http://127.0.0.1:8317")
    parser.add_argument("--model", default="zk-auto", help="model id or alias")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--stream", action="store_true", help="benchmark the streaming path")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    runs: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=180) as client:
        for index in range(args.runs):
            prompt = args.prompt or PROMPTS[index % len(PROMPTS)]
            result = await run_once(
                client,
                base_url=args.base_url.rstrip("/"),
                model=args.model,
                prompt=prompt,
                max_tokens=args.max_tokens,
                stream=args.stream,
            )
            result["prompt"] = prompt[:60]
            runs.append(result)
            print(
                f"run {index + 1}/{args.runs}: status={result['status']} "
                f"{result['latency_ms']:.0f}ms provider={result['provider']} "
                f"cred={result['credential']} attempt={result['attempt']}"
            )

    latencies = [run["latency_ms"] for run in runs if run["status"] == 200]
    summary = {
        "model": args.model,
        "runs": len(runs),
        "success": sum(1 for run in runs if run["status"] == 200),
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 1) if latencies else 0.0,
            "p50": round(percentile(latencies, 0.5), 1),
            "p95": round(percentile(latencies, 0.95), 1),
            "min": round(min(latencies), 1) if latencies else 0.0,
            "max": round(max(latencies), 1) if latencies else 0.0,
        },
        "total_completion_tokens": sum(
            int((run["usage"] or {}).get("completion_tokens") or 0) for run in runs
        ),
        "runs_detail": runs,
    }
    print(json.dumps(summary, indent=2))
    return 0 if summary["success"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
