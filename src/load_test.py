"""Routing latency under load (V1-18; plan §3: "p99 routing latency, per decision,
at concurrency 1 / 8 / 32").

Replays rendered prompts (prompts-c<N>.jsonl, the files generate.py reads) at
the router service, with N requests in flight at once, and reports p50/p95/p99
of the end-to-end time per decision, plus throughput. Run the service with
--no-teacher: the number measured is the router's own cost (render, student
generation with log-probs, scoring, routing), never a teacher API round trip,
and the test spends no quota.

Each level gets a short warm-up that is not measured (vLLM's first batches
compile kernels and fill the prefix cache).

    python src/load_test.py --url http://127.0.0.1:8080 \\
        --prompts /kaggle/input/<dataset>/prompts-c80.jsonl --label qwen15b-sft-c80
"""

from __future__ import annotations

import json
import math
import sys
import time
import urllib.request
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LEVELS = (1, 8, 32)


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile: the smallest value with at least q% of the
    values at or below it. No interpolation, so p99 is a latency that happened."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize(latencies_ms: Sequence[float], wall_s: float, errors: int) -> dict[str, Any]:
    return {
        "requests": len(latencies_ms) + errors,
        "errors": errors,
        "p50_ms": round(percentile(latencies_ms, 50), 1),
        "p95_ms": round(percentile(latencies_ms, 95), 1),
        "p99_ms": round(percentile(latencies_ms, 99), 1),
        "mean_ms": round(sum(latencies_ms) / len(latencies_ms), 1),
        "throughput_rps": round(len(latencies_ms) / wall_s, 2) if wall_s > 0 else None,
    }


def post_route(url: str, prompt: str, timeout_s: float = 600.0) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{url.rstrip('/')}/route",
        data=json.dumps({"prompt": prompt}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        return json.loads(r.read())


def run_level(
    send: Callable[[str], dict[str, Any]], prompts: Sequence[str], concurrency: int
) -> dict[str, Any]:
    """Every prompt once, `concurrency` in flight. Latency is client-side, so it
    includes queueing at the server: what a caller of the router would see."""

    def one(prompt: str) -> tuple[float | None, dict[str, Any] | None]:
        started = time.perf_counter()
        try:
            body = send(prompt)
        except Exception:
            return None, None
        return (time.perf_counter() - started) * 1000, body

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(one, prompts))
    wall = time.perf_counter() - started
    ok = [(ms, b) for ms, b in results if ms is not None and b is not None]
    if not ok:
        raise RuntimeError(f"every request failed at concurrency {concurrency}")
    out = summarize([ms for ms, _ in ok], wall, len(results) - len(ok))
    student = [b["student_ms"] for _, b in ok if b.get("student_ms") is not None]
    out["student_p50_ms"] = round(percentile(student, 50), 1) if student else None
    out["escalation_rate"] = round(sum(bool(b.get("escalated")) for _, b in ok) / len(ok), 4)
    return {"concurrency": concurrency, **out}


def main(argv: list[str] | None = None) -> int:
    import argparse

    from src.runlog import read_records

    parser = argparse.ArgumentParser(description="Router latency at concurrency 1/8/32")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--prompts", type=Path, required=True)
    parser.add_argument("--label", required=True, help="names the output, e.g. qwen15b-sft-c80")
    parser.add_argument("--per-level", type=int, default=256, help="requests measured per level")
    parser.add_argument("--warmup", type=int, default=16)
    parser.add_argument("--levels", type=int, nargs="+", default=list(LEVELS))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    prompts = [r["prompt"] for r in read_records(args.prompts)]
    if len(prompts) < args.per_level + args.warmup:
        parser.error(
            f"{args.prompts} has {len(prompts)} prompts; need {args.per_level + args.warmup}"
        )
    # A fixed spread over the file, so each level sees the same mix of short and long contexts.
    stride = len(prompts) / (args.per_level + args.warmup)
    picked = [prompts[int(i * stride)] for i in range(args.per_level + args.warmup)]
    warm, measured = picked[: args.warmup], picked[args.warmup :]

    def send(prompt: str) -> dict[str, Any]:
        return post_route(args.url, prompt)

    levels = []
    for c in args.levels:
        run_level(send, warm, c)
        row = run_level(send, measured, c)
        levels.append(row)
        print(
            f"[load_test] concurrency {c:>2}: p50 {row['p50_ms']:.0f} ms, p99 {row['p99_ms']:.0f} ms, "
            f"{row['throughput_rps']} req/s, {row['errors']} errors"
        )
    report = {"label": args.label, "prompts": str(args.prompts), "levels": levels}
    out = args.out or Path("results") / f"load-test-{args.label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"[load_test] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
