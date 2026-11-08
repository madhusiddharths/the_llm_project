"""Stage F4: merge a LoRA adapter into its base and quantize it to GPTQ 4-bit
(W4A16, group size 128), so vLLM can serve it on a T4 (DECIDED 2026-11-08,
Madhu: GPTQ). GPU, Kaggle (notebooks/kaggle_quantize.ipynb).

llm-compressor's GPTQ writes the compressed-tensors format, which vLLM reads
from the checkpoint's config (no --quantization flag). Calibration uses
TRAIN-split states (data/grpo/states.jsonl, built by make_pairs.py states), so
the forced-eval test prompts never shape the quantized weights. Each sample is
cut to its LAST `max_len` tokens: the decision point and the turns before it,
which is where the model's activations matter, rather than the shared policy
and catalog at the front.

    python src/quantize.py --config configs/qwen3b.yaml \\
        --adapter madhusiddharths1/tool-router-qwen3b-sft \\
        --states /kaggle/input/<dataset>/states.jsonl --out /kaggle/working/qwen3b-sft-gptq
    # --push <repo id> uploads the folder; a smoke run: --smoke --samples 8 --max-len 512
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cli import build_parser, resolve
from src.hashing import hash_file
from src.runlog import read_records

SCHEME = "W4A16"  # 4-bit weights, fp16 activations; group size 128 (llm-compressor default)
SAMPLES, MAX_LEN = 128, 2048


def calibration_prompts(states: Path, n: int, seed: int) -> list[str]:
    """n train-split prompts, a seeded sample, in a fixed order."""
    prompts = [r["prompt"] for r in read_records(states)]
    if len(prompts) < n:
        raise ValueError(f"{states} has {len(prompts)} states; need {n}")
    return random.Random(seed).sample(prompts, n)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("Merge a LoRA adapter and quantize it to GPTQ W4A16 (GPU)")
    parser.add_argument("--adapter", required=True, help="Hub repo id or local dir")
    parser.add_argument("--states", type=Path, required=True, help="train-split states.jsonl")
    parser.add_argument("--samples", type=int, default=SAMPLES)
    parser.add_argument("--max-len", type=int, default=MAX_LEN)
    parser.add_argument("--push", help="Hub repo id to upload the quantized folder to")
    args = parser.parse_args(argv)
    if args.out is None:
        parser.error("--out (a directory) is required")
    cfg = resolve(args)
    n, max_len = args.samples, args.max_len

    import llmcompressor
    import torch
    from datasets import Dataset
    from llmcompressor import oneshot
    from llmcompressor.modifiers.quantization import GPTQModifier
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    from src.registry import resolve_adapter
    from src.train import _push

    started = time.time()
    base = cfg.model.base_model
    tok = AutoTokenizer.from_pretrained(base)
    # One T4 holds even 3B in fp16 (~6 GB) with room for calibration activations.
    model = AutoModelForCausalLM.from_pretrained(
        base, torch_dtype=torch.float16, device_map="cuda:0"
    )
    model = PeftModel.from_pretrained(model, resolve_adapter(args.adapter)).merge_and_unload()

    def encode(prompt: str) -> dict[str, Any]:
        ids = tok(prompt, add_special_tokens=False)["input_ids"][-max_len:]
        return {"input_ids": ids, "attention_mask": [1] * len(ids)}

    data = Dataset.from_list([encode(p) for p in calibration_prompts(args.states, n, cfg.seed)])
    oneshot(
        model=model,
        dataset=data,
        recipe=GPTQModifier(targets="Linear", scheme=SCHEME, ignore=["lm_head"]),
        max_seq_length=max_len,
        num_calibration_samples=n,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(args.out, save_compressed=True)
    tok.save_pretrained(args.out)

    meta = {
        "base_model": base,
        "adapter": args.adapter,
        "method": "gptq",
        "scheme": SCHEME,
        "format": "compressed-tensors",
        "calibration": {
            "states": args.states.name,
            "states_sha256": hash_file(args.states),
            "samples": n,
            "max_len": max_len,
            "seed": cfg.seed,
        },
        "versions": {"llmcompressor": llmcompressor.__version__, "torch": torch.__version__},
        "smoke": cfg.smoke,
        "seconds": round(time.time() - started, 1),
    }
    (args.out / "quantize.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"[quantize] {args.adapter} -> {args.out} in {meta['seconds']}s")
    _push(args.push, args.out, ".")  # no-op without --push
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
