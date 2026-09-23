"""LoRA fine-tuning on the SFT set (plan §6 week 2, V1-08). Runs on Kaggle.

Identical data and hyperparameters across rungs; only the base model changes.
Runs on Kaggle, never on the Mac: one canonical training path (plan §7). The
Mac runs the unit tests (tests/test_train.py), which train a tiny random Qwen2
for one step on CPU through exactly this code.

What it guarantees before spending GPU time:
  - The dataset is the one the config expects: manifest.json must match the
    config's prompt_template_hash, system_prompt_hash, catalog-16 hash and
    teacher fingerprint, and train.jsonl must match the manifest's sha256.
  - Train and eval see the SAME TOKENS (review 2026-09-22). The episode text is
    tokenized once, whole, and the loss mask comes from character offsets. It is
    never built by tokenizing segments separately and concatenating, which
    could shift a token boundary. check_alignment() then proves, for every
    teacher decision, that tokenizing the eval prompt alone gives exactly the
    tokens that precede that decision in the training sequence.
  - Nothing is truncated. An episode longer than train.max_seq_len is an error;
    D2b set 12288 so that every harvested episode (max 12,176) fits.

Memory on a T4 (16 GB, no bf16): the base model runs in fp16 under autocast,
LoRA weights stay fp32, a GradScaler handles fp16 gradients, and gradient
checkpointing is on. The loss computes logits only at the ~10% of positions
that carry a label, so the 151,936-way vocabulary never materialises for all
12k positions (that alone would be ~7 GB in fp32). K/V heads are expanded to
the query count before SDPA, without which attention falls back to the math
backend and OOMs — see force_sdpa_kv_expansion().

Checkpoints each epoch to --output-dir and, with --hub-repo, to the HF Hub
(checkpoints/epoch-N/), and resumes from the latest one, because Kaggle
sessions die at 12 hours (plan §7).

    python src/train.py --config configs/qwen05b.yaml --data /kaggle/input/<dataset> \\
        --output-dir /kaggle/working/qwen05b --hub-repo <user>/tool-router-qwen05b --wandb
    # smoke: one optimizer step on the longest episode (T4 memory check), no Hub push
    python src/train.py --config configs/qwen05b.yaml --data <dir> --output-dir /tmp/s --smoke
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cli import build_parser, resolve
from src.config import ExperimentConfig
from src.hashing import hash_file
from src.invariants import InvariantViolation, check_all

LORA_TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]
WARMUP_FRACTION = 0.1
MAX_GRAD_NORM = 1.0
IGNORE = -100


# --- data --------------------------------------------------------------------


def load_sft(data_dir: Path, cfg: ExperimentConfig) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """train.jsonl + manifest.json, refused unless they match this config."""
    suffix = ".smoke" if cfg.smoke else ""
    manifest_path = data_dir / f"manifest{suffix}.json"
    if not manifest_path.exists() and cfg.smoke:
        manifest_path = data_dir / "manifest.json"  # a smoke run may use the full set
    manifest = json.loads(manifest_path.read_text())
    data_path = data_dir / manifest["file"]

    expected = {
        "prompt_template_hash": cfg.prompt_template_hash,
        "system_prompt_hash": cfg.system_prompt_hash,
        "catalog_hash": cfg.eval.catalog_hashes.get(16),
        "teacher_fingerprint": cfg.teacher_fingerprint(),
    }
    for key, want in expected.items():
        if manifest.get(key) != want:
            raise InvariantViolation(
                f"{manifest_path}: {key} is {str(manifest.get(key))[:12]}, config wants "
                f"{str(want)[:12]}. Rebuild with build_sft.py and re-upload."
            )
    if hash_file(data_path) != manifest["file_sha256"]:
        raise InvariantViolation(f"{data_path} does not match its manifest sha256")
    records = [json.loads(line) for line in data_path.read_text().splitlines() if line.strip()]
    return records, manifest


@dataclass(frozen=True)
class Encoded:
    key: str  # "<task>:<rep>"
    input_ids: list[int]
    labels: list[int]  # IGNORE outside teacher decisions

    @property
    def n_target(self) -> int:
        return sum(1 for y in self.labels if y != IGNORE)


class TokenBoundaryError(ValueError):
    """A token straddles the edge of a trainable span: train/eval would diverge."""


def _train_spans(segments: list[list[Any]]) -> list[tuple[int, int]]:
    spans, pos = [], 0
    for text, train in segments:
        if train:
            spans.append((pos, pos + len(text)))
        pos += len(text)
    return spans


def encode_episode(tokenizer, record: dict[str, Any]) -> Encoded:
    """Tokenize the whole episode once; label tokens whose characters fall
    inside a trainable segment. See the module docstring for why."""
    text = "".join(t for t, _ in record["segments"])
    spans = _train_spans(record["segments"])
    enc = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
    ids, offsets = enc["input_ids"], enc["offset_mapping"]
    labels = [IGNORE] * len(ids)
    j = 0
    for i, (a, b) in enumerate(offsets):
        while j < len(spans) and spans[j][1] <= a:
            j += 1
        if j == len(spans) or b <= spans[j][0]:
            continue  # wholly outside every trainable span
        s, e = spans[j]
        if a < s or b > e:
            raise TokenBoundaryError(
                f"episode {record['task_id']}:{record['rep']}: token {i} spans chars "
                f"{a}-{b}, across trainable span {s}-{e}"
            )
        labels[i] = ids[i]
    return Encoded(f"{record['task_id']}:{record['rep']}", list(ids), labels)


def check_alignment(tokenizer, record: dict[str, Any], encoded: Encoded) -> None:
    """For every decision: tokens(eval prompt) == the tokens before it here.

    The eval prompt is the episode text up to the decision (src/prompts.py
    guarantees serialize_state(prefix) is exactly that string; tests prove it).
    """
    text = "".join(t for t, _ in record["segments"])
    enc = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
    starts = [a for a, _ in enc["offset_mapping"]]
    for s, _ in _train_spans(record["segments"]):
        k = next(i for i, a in enumerate(starts) if a >= s)
        alone = tokenizer(text[:s], add_special_tokens=False)["input_ids"]
        if alone != encoded.input_ids[:k]:
            raise TokenBoundaryError(
                f"episode {encoded.key}: the eval prompt ending at char {s} tokenizes "
                "differently on its own than inside the training sequence"
            )


def encode_all(tokenizer, records, max_len: int, *, check: bool = True) -> list[Encoded]:
    encoded = []
    too_long = []
    for rec in records:
        e = encode_episode(tokenizer, rec)
        if check:
            check_alignment(tokenizer, rec, e)
        if len(e.input_ids) > max_len:
            too_long.append((e.key, len(e.input_ids)))
        encoded.append(e)
    if too_long:
        raise InvariantViolation(
            f"{len(too_long)} episodes exceed train.max_seq_len={max_len} (nothing is "
            f"truncated): {sorted(too_long, key=lambda kv: -kv[1])[:5]}"
        )
    return encoded


# --- model and loss ----------------------------------------------------------


def wrap_lora(cfg: ExperimentConfig, base_model):
    """LoRA on every attention and MLP projection; adapter weights in fp32."""
    from peft import LoraConfig, get_peft_model

    base_model.config.use_cache = False
    base_model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    base_model.enable_input_require_grads()
    model = get_peft_model(
        base_model,
        LoraConfig(
            r=cfg.train.lora_r,
            lora_alpha=cfg.train.lora_alpha,
            lora_dropout=cfg.train.lora_dropout,
            target_modules=LORA_TARGETS,
            task_type="CAUSAL_LM",
        ),
    )
    for p in model.parameters():
        if p.requires_grad:
            p.data = p.data.float()
    return model


def force_sdpa_kv_expansion(device) -> None:
    """Expand K/V to the query head count before SDPA, on pre-Ampere GPUs only.

    Qwen2.5 is grouped-query: 14 query heads against 2 KV heads at 0.5B, 12
    against 2 at 1.5B. Transformers hands those to SDPA unexpanded with
    enable_gqa=True, and on a T4 (sm75) no fused kernel will take that: flash
    needs sm80, and the memory-efficient kernel refuses broadcast GQA ("both
    fused kernels require query, key and value to have the same num_heads").
    SDPA then falls back to the math backend, which materialises the full
    [1, 14, 12176, 12176] fp16 score matrix — 7.7 GiB for a single worst-case
    episode, and an OOM on a 16 GB card.

    repeat_kv costs ~22 MB and keeps SDPA on the memory-efficient kernel. The
    arithmetic is identical; only the layout changes. Measured on a Kaggle T4,
    2026-09-23, one forward+backward at 12,176 tokens: 5.43 GiB peak at 0.5B
    and 8.38 GiB at 1.5B, against 14.56 GiB of card.

    Ampere and later have the flash kernel, which handles GQA natively and is
    faster than expanding, so they are left alone.
    """
    import torch

    if device.type != "cuda" or torch.cuda.get_device_capability(device) >= (8, 0):
        return
    from transformers.integrations import sdpa_attention

    sdpa_attention.use_gqa_in_sdpa = lambda *_args, **_kwargs: False


def load_base_model(cfg: ExperimentConfig, device):
    import torch
    from transformers import AutoModelForCausalLM

    force_sdpa_kv_expansion(device)
    dtype = torch.float16 if device.type == "cuda" else torch.float32
    model = AutoModelForCausalLM.from_pretrained(
        cfg.model.base_model, torch_dtype=dtype, attn_implementation="sdpa"
    )
    return model.to(device)


def masked_lm_loss(model, input_ids, labels):
    """Summed next-token loss over labelled positions only, and their count.

    Logits are computed for labelled positions alone: hidden states are
    gathered first, then projected, instead of projecting all T positions.
    """
    import torch.nn.functional as F

    base = model.get_base_model()
    hidden = base.model(input_ids=input_ids).last_hidden_state
    targets = labels[:, 1:]
    mask = targets != IGNORE
    logits = base.lm_head(hidden[:, :-1][mask]).float()
    return F.cross_entropy(logits, targets[mask], reduction="sum"), int(mask.sum())


# --- training loop -----------------------------------------------------------


def lr_lambda(total_steps: int):
    warmup = max(1, round(WARMUP_FRACTION * total_steps))

    def f(step: int) -> float:
        if step < warmup:
            return (step + 1) / warmup
        return max(0.0, (total_steps - step) / max(1, total_steps - warmup))

    return f


def _latest_checkpoint(out_dir: Path) -> Path | None:
    ckpts = sorted(out_dir.glob("checkpoint-epoch-*"), key=lambda p: int(p.name.rsplit("-", 1)[1]))
    return ckpts[-1] if ckpts else None


def _pull_hub_checkpoints(repo: str, out_dir: Path) -> None:
    """Restore checkpoints from the Hub into a fresh Kaggle session's out_dir."""
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import RepositoryNotFoundError

    try:
        snap = Path(snapshot_download(repo, allow_patterns=["checkpoints/*"]))
    except RepositoryNotFoundError:
        return
    for ckpt in sorted((snap / "checkpoints").glob("epoch-*")):
        dest = out_dir / f"checkpoint-epoch-{ckpt.name.split('-')[1]}"
        if not dest.exists():
            import shutil

            shutil.copytree(ckpt, dest)


def _push(repo: str | None, folder: Path, path_in_repo: str) -> None:
    if repo is None:
        return
    from huggingface_hub import HfApi

    api = HfApi()
    api.create_repo(repo, private=True, exist_ok=True)
    api.upload_folder(folder_path=str(folder), path_in_repo=path_in_repo, repo_id=repo)


def _print_log(msg: str, step: int | None = None, metrics: dict | None = None) -> None:
    print(msg, flush=True)


def train(
    cfg: ExperimentConfig,
    model,
    examples: list[Encoded],
    out_dir: Path,
    *,
    hub_repo: str | None = None,
    log=_print_log,
) -> dict[str, Any]:
    import torch
    from peft import set_peft_model_state_dict
    from safetensors.torch import load_file

    device = next(model.parameters()).device
    fp16 = device.type == "cuda"
    params = [p for p in model.parameters() if p.requires_grad]
    accum = cfg.train.batch_size * cfg.train.grad_accum
    steps_per_epoch = math.ceil(len(examples) / accum)
    total_steps = cfg.train.max_steps or steps_per_epoch * cfg.train.epochs

    opt = torch.optim.AdamW(params, lr=cfg.train.learning_rate, weight_decay=0.0)
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lr_lambda(total_steps))
    scaler = torch.amp.GradScaler("cuda", enabled=fp16)

    start_epoch, step = 0, 0
    if hub_repo:
        _pull_hub_checkpoints(hub_repo, out_dir)
    if (ckpt := _latest_checkpoint(out_dir)) is not None:
        set_peft_model_state_dict(model, load_file(str(ckpt / "adapter_model.safetensors")))
        state = torch.load(ckpt / "trainer_state.pt", map_location=device, weights_only=False)
        opt.load_state_dict(state["optimizer"])
        sched.load_state_dict(state["scheduler"])
        scaler.load_state_dict(state["scaler"])
        start_epoch, step = state["epoch"] + 1, state["global_step"]
        log(f"[train] resumed from {ckpt.name} (step {step})")

    model.train()
    last_loss = float("nan")
    for epoch in range(start_epoch, cfg.train.epochs):
        if step >= total_steps:
            break
        order = random.Random(cfg.seed + epoch).sample(range(len(examples)), len(examples))
        for g in range(0, len(order), accum):
            group = [examples[i] for i in order[g : g + accum]]
            n_tokens = sum(e.n_target for e in group)
            total = 0.0
            for e in group:
                ids = torch.tensor([e.input_ids], device=device)
                labels = torch.tensor([e.labels], device=device)
                with torch.autocast(device.type, dtype=torch.float16, enabled=fp16):
                    loss_sum, _ = masked_lm_loss(model, ids, labels)
                scaler.scale(loss_sum / n_tokens).backward()
                total += float(loss_sum.detach())
            scaler.unscale_(opt)
            torch.nn.utils.clip_grad_norm_(params, MAX_GRAD_NORM)
            scaler.step(opt)
            scaler.update()
            opt.zero_grad(set_to_none=True)
            sched.step()
            step += 1
            last_loss = total / n_tokens
            log(
                f"[train] epoch {epoch} step {step}/{total_steps} loss {last_loss:.4f} "
                f"lr {sched.get_last_lr()[0]:.2e} tokens {n_tokens}",
                step=step,
                metrics={"loss": last_loss, "lr": sched.get_last_lr()[0], "epoch": epoch},
            )
            if step >= total_steps:
                break

        ckpt = out_dir / f"checkpoint-epoch-{epoch}"
        model.save_pretrained(ckpt)
        torch.save(
            {
                "optimizer": opt.state_dict(),
                "scheduler": sched.state_dict(),
                "scaler": scaler.state_dict(),
                "epoch": epoch,
                "global_step": step,
            },
            ckpt / "trainer_state.pt",
        )
        _push(hub_repo, ckpt, f"checkpoints/epoch-{epoch}")
        log(f"[train] saved {ckpt.name}")

    final = out_dir / "final"
    model.save_pretrained(final)
    return {
        "global_step": step,
        "total_steps": total_steps,
        "final_loss": last_loss,
        "final_dir": str(final),
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("LoRA fine-tuning on the SFT set")
    parser.add_argument(
        "--data", type=Path, required=True, help="dir with train.jsonl + manifest.json"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--hub-repo", help="push checkpoints and the final adapter here")
    parser.add_argument("--wandb", action="store_true", help="log to Weights & Biases")
    args = parser.parse_args(argv)

    cfg = resolve(args)
    print(f"[train] {', '.join(check_all(cfg))}")
    import torch
    import transformers
    from transformers import AutoTokenizer

    records, manifest = load_sft(args.data, cfg)
    tokenizer = AutoTokenizer.from_pretrained(cfg.model.base_model)
    examples = encode_all(tokenizer, records, cfg.train.max_seq_len)
    if cfg.smoke:
        # The longest episodes, not the first: the one-step smoke run then also
        # proves the worst-case sequence (12,176 tokens) fits in T4 memory.
        examples = sorted(examples, key=lambda e: -len(e.input_ids))
        examples = examples[: cfg.train.batch_size * cfg.train.grad_accum]
    lengths = sorted(len(e.input_ids) for e in examples)
    print(
        f"[train] {len(examples)} episodes, {sum(e.n_target for e in examples)} target tokens, "
        f"lengths p50 {lengths[len(lengths) // 2]} max {lengths[-1]}; alignment checked"
    )

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = wrap_lora(cfg, load_base_model(cfg, device))
    model.print_trainable_parameters()

    run = None
    if args.wandb:
        import wandb

        run = wandb.init(
            project=os.environ.get("WANDB_PROJECT", "tool-router-ladder"),
            name=f"{cfg.name}-sft",
            config={**cfg.model_dump(mode="json"), "manifest": manifest},
        )

    def log(msg, step=None, metrics=None):
        print(msg, flush=True)
        if run is not None and metrics:
            run.log(metrics, step=step)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    result = train(cfg, model, examples, args.output_dir, hub_repo=args.hub_repo, log=log)

    summary = {
        **result,
        "config": cfg.name,
        "config_fingerprint": cfg.fingerprint(),
        "base_model": cfg.model.base_model,
        "smoke": cfg.smoke,
        "manifest": {
            k: manifest[k] for k in ("file_sha256", "prompt_template_hash", "teacher_fingerprint")
        },
        "n_episodes": len(examples),
        "seconds": round(time.time() - started, 1),
        "versions": {"torch": torch.__version__, "transformers": transformers.__version__},
        "hub_repo": args.hub_repo,
    }
    final = Path(result["final_dir"])
    (final / "run.json").write_text(json.dumps(summary, indent=2) + "\n")
    _push(args.hub_repo, final, ".")
    print(f"[train] done: {json.dumps(summary)}")
    if run is not None:
        run.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
