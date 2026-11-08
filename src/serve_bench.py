"""Stage F serving benchmarks (plan X6, F1-2): latency and throughput of one vLLM
server configuration, replaying recorded forced-eval prompts. No API quota.

One run = one server configuration (model, adapters, prefix caching, guided
decoding) x a concurrency sweep x `--repeats` repeats, because Kaggle GPUs are
shared and a latency needs an error bar (X6). Per level and repeat it reports
p50/p99 end-to-end latency and time to first token (streamed), requests/s and
generated tokens/s; across repeats, the mean and standard deviation of each.

What each Stage F measurement varies (notebooks/kaggle_serve_bench.ipynb):
  F2 multi-LoRA       --adapter sft=... --adapter grpo=... [--mix]
  F3 guided decoding  --guided (latency only; accuracy: generate.py --guided-catalog)
  F4 quantization     --model <quantize.py checkpoint> (accuracy: generate.py --model)
  F5 prefix caching   --no-prefix-caching, at catalogs 16/40/80
  F6 concurrency      --levels

Prompts: a seeded shuffle of the prompts file, dealt into disjoint blocks, one
per (repeat, level) plus a short warm-up each. No prompt is sent twice, so with
prefix caching on, a request can reuse only what real traffic shares (the
system prompt and the tool catalog, which grows with the catalog), never a
whole earlier prompt. Correctness is not measured here: accuracy runs go
through generate.py (offline, batched) and eval_forced.py, like every other
forced-eval number.

    python src/serve_bench.py --config configs/qwen3b.yaml --catalog 80 \\
        --prompts /kaggle/input/<dataset>/prompts-c80.jsonl \\
        --adapter sft=madhusiddharths1/tool-router-qwen3b-sft --label f6-qwen3b-sft-c80
    # offline plumbing check: --backend fake --smoke --levels 1 2 --per-level 4 --repeats 2
"""

from __future__ import annotations

import json
import random
import statistics
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cli import build_parser, resolve
from src.hashing import hash_file
from src.load_test import LEVELS, fan_out, percentile
from src.runlog import read_records
from src.serving import SERVED_NAME, SERVER_PORT, completion_request, stop_server

TIMEOUT_S = 600.0

Send = Callable[[int, str], dict[str, Any]]  # (request index, prompt) -> one measurement


def request_body(cfg, prompt: str, served: str, guided: str | None) -> dict[str, Any]:
    """serving.completion_request (greedy, the teacher's max_tokens), streamed so
    the first token can be timed, with token usage in the last chunk."""
    body = completion_request(cfg, prompt) | {
        "model": served,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if guided:
        body["structured_outputs"] = {"regex": guided}
    return body


def read_stream(lines, started: float) -> dict[str, Any]:
    """Time to first token, total latency and generated tokens from an SSE
    stream of /v1/completions chunks (`lines` yields raw bytes lines)."""
    ttft = tokens = None
    for raw in lines:
        line = raw.decode().strip()
        if not line.startswith("data: "):
            continue
        data = line[len("data: ") :]
        if data == "[DONE]":
            break
        chunk = json.loads(data)
        if ttft is None and any(c.get("text") for c in chunk.get("choices") or []):
            ttft = (time.perf_counter() - started) * 1000
        if chunk.get("usage"):
            tokens = chunk["usage"]["completion_tokens"]
    if tokens is None:
        raise ValueError("stream ended without a usage chunk")
    return {
        "latency_ms": (time.perf_counter() - started) * 1000,
        "ttft_ms": ttft,  # None when nothing but the stop token was generated
        "completion_tokens": tokens,
    }


def http_send(url: str, body_for: Callable[[int, str], dict[str, Any]]) -> Send:
    def send(i: int, prompt: str) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{url.rstrip('/')}/v1/completions",
            data=json.dumps(body_for(i, prompt)).encode(),
            headers={"Content-Type": "application/json"},
        )
        started = time.perf_counter()
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return read_stream(r, started)

    return send


def fake_send(i: int, prompt: str) -> dict[str, Any]:
    """--backend fake: a deterministic stand-in, so the sweep runs offline."""
    ms = 1 + len(prompt) / 1000
    return {"latency_ms": 2 * ms, "ttft_ms": ms, "completion_tokens": 8}


def deal(prompts: Sequence[str], sizes: Sequence[int], seed: int) -> list[list[str]]:
    """Disjoint blocks of the given sizes from a seeded shuffle of the prompts."""
    if sum(sizes) > len(prompts):
        raise ValueError(
            f"the sweep needs {sum(sizes)} distinct prompts; the file has {len(prompts)}"
        )
    order = random.Random(seed).sample(list(prompts), len(prompts))
    blocks, start = [], 0
    for n in sizes:
        blocks.append(order[start : start + n])
        start += n
    return blocks


def run_level(send: Send, prompts: Sequence[str], concurrency: int) -> dict[str, Any]:
    """Every prompt once, `concurrency` in flight; latency is client-side, so it
    includes queueing at the server."""

    def one(item: tuple[int, str]) -> dict[str, Any]:
        try:
            return send(*item)
        except Exception as e:  # printed here, counted as an error by fan_out
            print(f"[serve_bench] request failed: {e!r}")
            raise

    results, wall = fan_out(one, list(enumerate(prompts)), concurrency)
    ok = [r for r in results if r is not None]
    if not ok:
        raise RuntimeError(f"every request failed at concurrency {concurrency}")
    lat = [r["latency_ms"] for r in ok]
    ttft = [r["ttft_ms"] for r in ok if r["ttft_ms"] is not None]
    return {
        "requests": len(results),
        "errors": len(results) - len(ok),
        "p50_ms": round(percentile(lat, 50), 1),
        "p99_ms": round(percentile(lat, 99), 1),
        "ttft_p50_ms": round(percentile(ttft, 50), 1) if ttft else None,
        "ttft_p99_ms": round(percentile(ttft, 99), 1) if ttft else None,
        "requests_per_s": round(len(ok) / wall, 3),
        "tokens_per_s": round(sum(r["completion_tokens"] for r in ok) / wall, 1),
    }


def aggregate(repeats: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Mean and standard deviation across repeats of every numeric field."""
    out: dict[str, Any] = {}
    for key in repeats[0]:
        values = [r[key] for r in repeats if r[key] is not None]
        if not values:
            out[key] = None
            continue
        sd = statistics.stdev(values) if len(values) > 1 else None
        out[key] = {"mean": round(statistics.fmean(values), 3), "sd": sd and round(sd, 3)}
    return out


def block_sizes(levels: Sequence[int], per_level: int, warmup: int, repeats: int) -> list[int]:
    """deal's block sizes in sweep's order: (warm-up, measured) per level, per repeat."""
    return [n for _ in range(repeats) for _ in levels for n in (warmup, per_level)]


def sweep(
    send: Send, blocks: Iterator[list[str]], levels: Sequence[int], repeats: int
) -> list[dict[str, Any]]:
    """Repeat-major order (each repeat runs every level), so a slow patch on a
    shared GPU spreads over all levels instead of landing on one. `blocks` is
    deal(prompts, block_sizes(...)) for the same levels and repeats."""
    rows: dict[int, list[dict[str, Any]]] = {c: [] for c in levels}
    for r in range(repeats):
        for c in levels:
            warm, measured = next(blocks), next(blocks)
            if warm:
                run_level(send, warm, c)
            row = run_level(send, measured, c)
            rows[c].append(row)
            print(
                f"[serve_bench] repeat {r + 1}/{repeats} concurrency {c:>2}: p50 {row['p50_ms']:.0f} ms, "
                f"p99 {row['p99_ms']:.0f} ms, ttft p50 {row['ttft_p50_ms']} ms, "
                f"{row['requests_per_s']} req/s, {row['tokens_per_s']} tok/s, {row['errors']} errors"
            )
    return [{"concurrency": c, **aggregate(rows[c]), "repeats": rows[c]} for c in levels]


def _gpu() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=30, check=True,
        )  # fmt: skip
        return ", ".join(sorted(set(out.stdout.split("\n")) - {""}))
    except (OSError, subprocess.SubprocessError):
        return "none"


def _parse_adapters(items: Sequence[str]) -> dict[str, str]:
    out = {}
    for item in items:
        name, sep, repo = item.partition("=")
        if not sep or not name or not repo:
            raise ValueError(f"--adapter wants NAME=REPO, got {item!r}")
        out[name] = repo
    return out


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("Stage F: vLLM latency/throughput sweep over replayed prompts")
    parser.add_argument("--catalog", type=int, required=True, choices=[16, 40, 80])
    parser.add_argument(
        "--prompts", type=Path, required=True, help="forced-eval prompts-c<N>.jsonl"
    )
    parser.add_argument("--label", required=True, help="names the output, e.g. f6-qwen3b-sft-c80")
    parser.add_argument("--backend", choices=["vllm", "fake"], default="vllm")
    parser.add_argument(
        "--url", help="use a server that is already running instead of starting one"
    )
    parser.add_argument("--model", help="F4: a merged checkpoint instead of the base model")
    parser.add_argument(
        "--adapter", action="append", default=[], metavar="NAME=REPO", help="repeat for F2"
    )
    parser.add_argument("--mix", action="store_true", help="F2: round-robin over the adapters")
    parser.add_argument("--no-prefix-caching", action="store_true", help="F5")
    parser.add_argument("--guided", action="store_true", help="F3: prompts.guided_regex")
    parser.add_argument("--levels", type=int, nargs="+", default=list(LEVELS))
    parser.add_argument("--per-level", type=int, default=48, help="measured requests per level")
    parser.add_argument("--warmup", type=int, default=4, help="unmeasured requests per level")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-model-len", type=int, default=32768)
    args = parser.parse_args(argv)
    cfg = resolve(args)
    adapters = _parse_adapters(args.adapter)
    if args.model and adapters:
        parser.error("--model is a merged checkpoint; it takes no --adapter")
    if args.mix and len(adapters) < 2:
        parser.error("--mix needs at least two --adapter")

    guided = None
    if args.guided:
        from src.catalogs import guided_regex_for

        guided = guided_regex_for(cfg, args.catalog)
    names = list(adapters) or [SERVED_NAME]

    def body_for(i: int, prompt: str) -> dict[str, Any]:
        return request_body(cfg, prompt, names[i % len(names)] if args.mix else names[0], guided)

    prompts = [r["prompt"] for r in read_records(args.prompts)]
    sizes = block_sizes(args.levels, args.per_level, args.warmup, args.repeats)
    try:  # before a server start that can take minutes
        blocks = deal(prompts, sizes, cfg.seed)
    except ValueError as e:
        parser.error(f"{args.prompts}: {e}")
    suffix = ".smoke" if cfg.smoke else ""
    out = args.out or cfg.paths.results_dir / f"serve-bench-{args.label}{suffix}.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    server = None
    url = args.url or f"http://127.0.0.1:{SERVER_PORT}"
    version = None
    if args.backend == "fake":
        send: Send = fake_send
    else:
        from src.registry import resolve_adapter
        from src.serving import server_command, wait_ready

        if args.url is None:
            cmd = server_command(
                cfg,
                None,
                args.max_model_len,
                model=args.model,
                adapters={k: resolve_adapter(v) for k, v in adapters.items()},
                prefix_caching=not args.no_prefix_caching,
            )
            log = out.with_suffix(".vllm.log")
            print(f"[serve_bench] starting: {' '.join(cmd)} (log {log})")
            server = subprocess.Popen(cmd, stdout=log.open("w"), stderr=subprocess.STDOUT)
            try:
                wait_ready(url, timeout_s=1800, alive=lambda: server.poll() is None)
            except (RuntimeError, TimeoutError):
                stop_server(server)
                raise
        with urllib.request.urlopen(f"{url}/version", timeout=30) as r:
            version = json.loads(r.read()).get("version")
        send = http_send(url, body_for)

    started = time.time()
    try:
        levels = sweep(send, iter(blocks), args.levels, args.repeats)
    finally:
        if server is not None:
            stop_server(server)
    report = {
        "label": args.label,
        "catalog": args.catalog,
        "smoke": cfg.smoke,
        "settings": {
            "base_model": cfg.model.base_model,
            "model": args.model,
            "adapters": adapters,
            "mix": args.mix,
            "prefix_caching": not args.no_prefix_caching,
            "guided": args.guided,
            "max_tokens": cfg.teacher.max_tokens,
            "max_model_len": args.max_model_len,
            "per_level": args.per_level,
            "warmup": args.warmup,
            "repeats": args.repeats,
            "seed": cfg.seed,
        },
        "backend": args.backend,
        "vllm": version,
        "gpu": _gpu(),
        "prompts_file": args.prompts.name,
        "prompts_sha256": hash_file(args.prompts),
        "seconds": round(time.time() - started, 1),
        "levels": levels,
    }
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"[serve_bench] wrote {out} ({report['seconds']}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
