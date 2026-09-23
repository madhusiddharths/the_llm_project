"""Prompts in, completions out. The only step that needs a GPU for evaluation.

Deliberately dumb. It never builds a prompt and never scores one: the Mac
renders prompts (eval_forced.py --export) and scores completions
(eval_forced.py --backend file), so the serializer and the scoring rules can
only ever run in one place. This script maps text to text and records exactly
how it did it.

Backends:
  vllm (default)  Verified on Kaggle T4 on 2026-09-22: vLLM 0.29.0,
                  torch 2.13+cu130, TRITON_ATTN backend (FlashAttention-2
                  needs SM 8.0; T4 is 7.5), dtype=half.
  hf              transformers generate(), one prompt at a time. The fallback
                  if vLLM breaks (plan §10), slow but dependency-light.

Greedy decoding (temperature 0) and the teacher's max_tokens, so a completion
is a function of (model, adapter, prompt). Resumable: step_ids already in --out
are skipped, and results are appended after every chunk, because Kaggle
sessions die.

    # zero-shot 1.5B at catalog 16 (on Kaggle)
    python src/generate.py --config configs/qwen15b.yaml \\
        --prompts /kaggle/input/<dataset>/prompts-c16.jsonl \\
        --out /kaggle/working/completions-qwen15b-zeroshot-c16.jsonl
    # a fine-tuned student: add --adapter <hub repo id or local dir>
    # smoke: add --smoke --limit 3
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cli import build_parser, resolve
from src.hashing import hash_file
from src.prompts import IM_END
from src.runlog import read_records

CHUNK = 128  # prompts per vLLM call between appends
DEFAULT_MAX_MODEL_LEN = 32768  # Qwen2.5 context; catalog 80 prompts reach ~20k tokens


def pending_prompts(prompts_path: Path, out_path: Path, limit: int | None) -> list[dict[str, str]]:
    done = {r["step_id"] for r in read_records(out_path)} if out_path.exists() else set()
    rows = [r for r in read_records(prompts_path)]
    if limit is not None:
        rows = rows[:limit]
    ids = [r["step_id"] for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{prompts_path} repeats step_ids")
    return [r for r in rows if r["step_id"] not in done]


def append(out_path: Path, records: Iterable[dict[str, Any]]) -> None:
    with out_path.open("a") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()


def _resolve_adapter(adapter: str | None) -> str | None:
    """A Hub repo id becomes a local snapshot; vLLM's LoRARequest wants a path."""
    if adapter is None or Path(adapter).exists():
        return adapter
    from huggingface_hub import snapshot_download

    return snapshot_download(adapter, allow_patterns=["adapter_*", "*.json"])


def run_vllm(cfg, rows, out_path: Path, adapter: str | None, max_model_len: int) -> dict[str, Any]:
    import vllm
    from vllm import LLM, SamplingParams

    llm = LLM(
        model=cfg.model.base_model,
        dtype="half",
        max_model_len=max_model_len,
        gpu_memory_utilization=0.9,
        seed=cfg.seed,
        enable_lora=adapter is not None,
        max_lora_rank=cfg.train.lora_r,
    )
    params = SamplingParams(
        temperature=0.0, max_tokens=cfg.teacher.max_tokens, stop=[IM_END], seed=cfg.seed
    )
    lora = None
    if adapter is not None:
        from vllm.lora.request import LoRARequest

        lora = LoRARequest("student", 1, adapter)

    for start in range(0, len(rows), CHUNK):
        chunk = rows[start : start + CHUNK]
        outs = llm.generate([r["prompt"] for r in chunk], params, lora_request=lora)
        append(
            out_path,
            (
                {
                    "step_id": r["step_id"],
                    "completion": o.outputs[0].text,
                    "prompt_tokens": len(o.prompt_token_ids),
                    "completion_tokens": len(o.outputs[0].token_ids),
                    "finish_reason": o.outputs[0].finish_reason,
                }
                for r, o in zip(chunk, outs, strict=True)
            ),
        )
        print(f"[generate] {start + len(chunk)}/{len(rows)}")
    return {"vllm": vllm.__version__}


def run_hf(cfg, rows, out_path: Path, adapter: str | None, max_model_len: int) -> dict[str, Any]:
    import torch
    import transformers
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(cfg.model.base_model)
    model = AutoModelForCausalLM.from_pretrained(cfg.model.base_model, torch_dtype=torch.float16)
    if adapter is not None:
        from peft import PeftModel

        model = PeftModel.from_pretrained(model, adapter)
    model = model.to("cuda").eval()
    stop = tok.convert_tokens_to_ids(IM_END)
    for i, r in enumerate(rows):
        ids = tok(r["prompt"], return_tensors="pt", add_special_tokens=False).input_ids
        if ids.shape[1] > max_model_len:
            raise ValueError(f"{r['step_id']}: prompt is {ids.shape[1]} tokens > {max_model_len}")
        with torch.no_grad():
            out = model.generate(
                ids.to("cuda"),
                do_sample=False,
                max_new_tokens=cfg.teacher.max_tokens,
                eos_token_id=stop,
                pad_token_id=tok.pad_token_id or stop,
            )
        new = out[0, ids.shape[1] :]
        append(
            out_path,
            [
                {
                    "step_id": r["step_id"],
                    "completion": tok.decode(new, skip_special_tokens=False),
                    "prompt_tokens": int(ids.shape[1]),
                    "completion_tokens": int(new.shape[0]),
                    "finish_reason": "stop" if int(new[-1]) == stop else "length",
                }
            ],
        )
        if (i + 1) % 25 == 0:
            print(f"[generate] {i + 1}/{len(rows)}")
    return {"transformers": transformers.__version__}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("Prompts JSONL -> completions JSONL (GPU)")
    parser.add_argument("--prompts", type=Path, required=True)
    parser.add_argument("--adapter", help="LoRA adapter: Hub repo id or local directory")
    parser.add_argument("--backend", choices=["vllm", "hf"], default="vllm")
    parser.add_argument("--max-model-len", type=int, default=DEFAULT_MAX_MODEL_LEN)
    parser.add_argument("--limit", type=int, help="first N prompts only (use with --smoke)")
    args = parser.parse_args(argv)
    if args.out is None:
        parser.error("--out is required")

    cfg = resolve(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    rows = pending_prompts(args.prompts, args.out, args.limit)
    print(f"[generate] {cfg.model.base_model} adapter={args.adapter} backend={args.backend}")
    print(f"[generate] {len(rows)} prompts to run -> {args.out}")
    adapter = _resolve_adapter(args.adapter)

    started = time.time()
    versions: dict[str, Any] = {}
    if rows:
        runner = run_vllm if args.backend == "vllm" else run_hf
        versions = runner(cfg, rows, args.out, adapter, args.max_model_len)

    meta = {
        "base_model": cfg.model.base_model,
        "adapter": args.adapter,
        "backend": args.backend,
        "versions": versions,
        "sampling": {"temperature": 0.0, "max_tokens": cfg.teacher.max_tokens, "seed": cfg.seed},
        "max_model_len": args.max_model_len,
        "prompts_file": args.prompts.name,
        "prompts_sha256": hash_file(args.prompts),
        "config_fingerprint": cfg.fingerprint(),
        "seconds": round(time.time() - started, 1),
    }
    args.out.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"[generate] done in {meta['seconds']}s; meta -> {args.out.with_suffix('.meta.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
