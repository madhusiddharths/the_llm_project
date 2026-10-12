"""The student on Apple silicon with MLX (lane 2 of the parallel schedule, 2026-10-11).

Moves student inference (free-running episodes, Stage D, forced-eval
generation) from Kaggle's T4 to the Mac, so the weekly GPU allowance is left
for training. Same model, same adapter, same greedy decoding and stop token as
the vLLM paths; a different engine, so it is calibrated before use: the 1.5B
SFT forced eval at catalog 16 must match vLLM's 73.3% step agreement within
about a point, with nearly all completions identical (src/engine_check.py).
Results carry backend "mlx" in their identity and never share a cell with vLLM.

Choices, each one a place this could differ from vLLM:

1. WEIGHTS. MLX loads a merged checkpoint: the LoRA adapter folded into the
   base weights (merge_adapter below, peft on the CPU, the delta computed in
   fp32 and stored in fp16). vLLM applies the same adapter unmerged. The two
   differ only in fp16 rounding.
2. DTYPE fp16, as vLLM's --dtype half on the T4 (the checkpoints are bf16).
3. GREEDY. The argmax of the raw log-probs (mlx_lm computes log-softmax of the
   logits before any sampler), stopping at <|im_end|> or <|endoftext|>, at most
   teacher.max_tokens tokens. As vLLM: temperature 0, stop at im_end.
4. TEXT is the generated tokens decoded with special tokens skipped and the
   stop token dropped: what vLLM returns as `text`. src/logprobs.py re-decodes
   the token ids itself, so confidence never depends on this string.
5. LOG-PROBS are the emitted token's raw log-prob (fp16 logits, log-softmax in
   fp32, as vLLM's sampler does), plus the top FIRST_TOP_K
   alternatives at position 0, keyed by token id: generate.py --logprobs's
   record, field for field.
6. PREFIX REUSE. A turn's prompt mostly repeats the previous turn's, so the KV
   cache is kept and trimmed back to the longest common token prefix instead
   of re-reading ~12-20k tokens each time. This changes how much is computed,
   not what: attention over the same tokens. A small pool of caches (one per
   concurrent episode) is matched by longest prefix, least recently used out.
7. ONE MODEL, ONE LOCK. MLX evaluates one request at a time; tau2's worker
   threads queue on the lock, and the GPU works on one episode while the
   others wait on the simulator API.

Pure Python at import time; mlx, mlx_lm, torch and peft are imported inside
the functions that need them (tests/test_core_is_light.py).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.prompts import IM_END
from src.serving import FIRST_TOP_K

END_OF_TEXT = "<|endoftext|>"
MERGED_DIR = Path("models/mlx")


# --- the prefix cache pool (pure) -------------------------------------------------


def common_prefix(a: Sequence[int], b: Sequence[int]) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


@dataclass
class Slot:
    """One KV cache and the tokens it holds, in order."""

    cache: Any = None
    tokens: list[int] = field(default_factory=list)
    used: float = 0.0


class CachePool:
    """Pick the cache to continue from: the one sharing the longest token prefix
    with the new prompt. An episode's own slot wins on its next turn (it shares
    the whole conversation so far); a new episode shares only the system prompt
    and catalog with every slot, and ties go to the least recently used slot,
    usually a finished episode's, which keeps that shared prefix. With nothing
    shared at all: an empty slot, else the least recently used."""

    def __init__(self, size: int):
        if size < 1:
            raise ValueError("a pool needs at least one slot")
        self.slots = [Slot() for _ in range(size)]

    def choose(self, tokens: Sequence[int]) -> tuple[Slot, int]:
        best = max(self.slots, key=lambda s: (common_prefix(s.tokens, tokens), -s.used))
        shared = common_prefix(best.tokens, tokens)
        if shared == 0:
            best = min(self.slots, key=lambda s: (len(s.tokens) > 0, s.used))
        best.used = time.monotonic()
        return best, shared


def reusable(shared: int, prompt_len: int) -> int:
    """How many cached tokens to keep. At least one prompt token must be fed
    again: the next token's log-probs come from running the model on it."""
    return min(shared, prompt_len - 1)


# --- one generation ------------------------------------------------------------


@dataclass(frozen=True)
class Generation:
    text: str  # what vLLM returns as `text` (choice 4)
    token_ids: list[int]  # every emitted token, the stop token included
    token_logprobs: list[float]  # raw log-prob of each emitted token
    first_alternatives: dict[int, float]  # top FIRST_TOP_K at position 0
    finish_reason: str  # "stop" | "length"
    prompt_tokens: int
    reused_tokens: int  # prompt tokens served from the cache


class MLXEngine:
    """One student model on the Mac's GPU. Thread-safe: requests serialize on a lock."""

    def __init__(self, model_path: str | Path, *, max_tokens: int, pool_size: int = 1):
        import mlx.core as mx
        from mlx_lm import load

        self.model, wrapper = load(str(model_path))
        self.model.set_dtype(mx.float16)  # choice 2
        self.tokenizer = wrapper._tokenizer  # the Hugging Face tokenizer underneath
        self.stop_ids = {
            self.tokenizer.convert_tokens_to_ids(IM_END),
            self.tokenizer.convert_tokens_to_ids(END_OF_TEXT),
        }
        self.max_tokens = max_tokens
        self.pool = CachePool(pool_size)
        self.lock = threading.Lock()

    def encode(self, prompt: str) -> list[int]:
        return list(self.tokenizer(prompt, add_special_tokens=False)["input_ids"])

    def decode(self, ids: Sequence[int]) -> str:
        """With special tokens: what src/logprobs.py scores (its choice 7)."""
        return self.tokenizer.decode(
            list(ids), skip_special_tokens=False, clean_up_tokenization_spaces=False
        )

    def generate(self, prompt: str, *, top_k: int = FIRST_TOP_K) -> Generation:
        import mlx.core as mx
        from mlx_lm.generate import generate_step
        from mlx_lm.models.cache import make_prompt_cache, trim_prompt_cache

        ids = self.encode(prompt)
        if not ids:
            raise ValueError("empty prompt")
        with self.lock:
            slot, shared = self.pool.choose(ids)
            if slot.cache is None:
                slot.cache, slot.tokens = make_prompt_cache(self.model), []
            keep = reusable(shared, len(ids))
            if len(slot.tokens) > keep:
                trim_prompt_cache(slot.cache, len(slot.tokens) - keep)
            out, logprobs, first = [], [], {}
            finish = "length"
            steps = generate_step(
                mx.array(ids[keep:]),
                self.model,
                max_tokens=self.max_tokens,
                sampler=lambda lp: mx.argmax(lp, axis=-1),  # choice 3
                # Log-softmax in fp32, as vLLM does (its sampler upcasts the fp16
                # logits); in fp16 the log-probs come out in steps of ~1/32.
                logits_processors=[lambda _tokens, logits: logits.astype(mx.float32)],
                prompt_cache=slot.cache,
            )
            for token, lp in steps:
                token = int(token)
                if not out:
                    top = mx.argpartition(-lp, kth=top_k - 1)[:top_k]
                    top_ids: list[int] = top.tolist()  # type: ignore[assignment]
                    first = {int(t): float(lp[t]) for t in top_ids}
                out.append(token)
                logprobs.append(float(lp[token]))
                if token in self.stop_ids:
                    finish = "stop"
                    break
            # generate_step has fed every token it yielded into the cache (it
            # runs one step ahead), so the cache now holds prompt + output.
            slot.tokens = (ids + out)[: slot.cache[0].offset]
        body = out[:-1] if finish == "stop" else out
        text = self.tokenizer.decode(body, skip_special_tokens=True)
        return Generation(text, out, logprobs, first, finish, len(ids), keep)


# --- the merged checkpoint MLX loads ----------------------------------------------


def merged_model_dir(cfg, adapter: str | None, root: Path = MERGED_DIR) -> str:
    """The checkpoint for `adapter` on `cfg`'s base model: the base model's Hub id
    when there is no adapter (mlx_lm loads it directly), else a merged copy under
    models/mlx/, built once and reused. Keyed by the adapter's weights, so a
    retrained adapter under the same repo name gets a fresh merge."""
    if adapter is None:
        return cfg.model.base_model
    from src.generate import _resolve_adapter

    src = Path(_resolve_adapter(adapter) or adapter)
    weights = src / "adapter_model.safetensors"
    digest = hashlib.sha256(weights.read_bytes()).hexdigest()
    out = root / f"{cfg.name}-{digest[:12]}"
    if not (out / "merge.json").exists():
        merge_adapter(cfg.model.base_model, src, out)
        (out / "merge.json").write_text(
            json.dumps(
                {
                    "base_model": cfg.model.base_model,
                    "adapter": adapter,
                    "adapter_sha256": digest,
                    "dtype": "float16",
                },
                indent=2,
            )
            + "\n"
        )
    return str(out)


def merge_adapter(base_model: str, adapter_dir: Path, out: Path) -> None:
    """Fold the LoRA adapter into fp16 base weights on the CPU (choice 1). peft
    computes each delta in fp32 on the CPU before adding it to the fp16 weight."""
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    print(f"[mlx] merging {adapter_dir} into {base_model} -> {out}")
    model = AutoModelForCausalLM.from_pretrained(base_model, torch_dtype=torch.float16)
    merged = PeftModel.from_pretrained(model, str(adapter_dir)).merge_and_unload()
    out.mkdir(parents=True, exist_ok=True)
    merged.save_pretrained(out, safe_serialization=True)
    AutoTokenizer.from_pretrained(base_model).save_pretrained(out)
