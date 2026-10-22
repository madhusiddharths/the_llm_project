"""Stage E3: DPO on the students' own mistakes (plan X5). Runs on Kaggle.

A branch from the SFT checkpoint, never a stack (plan X4): the parent adapter's
weights must hash to an entry in configs/sft_registry.yaml
(invariants.assert_parent_is_sft) before any GPU time is spent.

Choices that differ from the plan's sketch, and why (review these):
  1. NOT TRL's DPOTrainer. It computes logits over every position of both
     sequences. Our prompts reach ~12k tokens at catalog 16, and a full
     151,936-way fp32 logit tensor at that length is ~7 GB per sequence, which
     no T4 holds. This loop reuses train.py's machinery instead: one
     tokenization with offset-based masks plus the alignment check (so DPO sees
     exactly the tokens eval sees), logits only at the labelled positions, fp16
     autocast with fp32 LoRA weights and a GradScaler, gradient checkpointing,
     the GQA fix for sm75, optional layer sharding for 3B, and per-epoch Hub
     checkpoints with resume.
  2. init = "continue_sft_adapter": the policy IS the SFT LoRA, trained further;
     the reference is that adapter frozen at step 0, whose log-probs are
     computed once before training and cached. The output is again one LoRA over
     the base model (same rank and targets), so vLLM, MLX, eval_forced and
     eval_free take it with no change. The plan's two options (a new LoRA on top
     of the adapter, or on merged weights) would each need a merged base model
     for every downstream engine.
  3. One sequence in memory at a time. The DPO loss couples the chosen and
     rejected sequences, but its gradient factorises:
         L = -log sigmoid(z),  z = beta * ((pc - rc) - (pr - rr))
         dL/dpc = -beta * sigmoid(-z),   dL/dpr = +beta * sigmoid(-z)
     so a no-grad pass gives z, and each sequence then gets its own backward
     scaled by its coefficient. Same gradient as backpropagating the joint loss,
     at the memory of one SFT step. LoRA dropout is set to 0 so the no-grad pass
     and the backward pass see the same function.
  4. Sequence log-prob = SUM over the completion's tokens (standard DPO), and
     the completion includes <|im_end|>, as in SFT.

    python src/train_dpo.py --config configs/v2/qwen15b-dpo.yaml --data <pairs dir> \\
        --parent madhusiddharths1/tool-router-qwen15b-sft \\
        --output-dir /kaggle/working/qwen15b-dpo --hub-repo <user>/tool-router-qwen15b-dpo --wandb
    # smoke: one optimizer step on the longest pair (T4 memory check), no Hub push
    python src/train_dpo.py ... --smoke
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
from src.invariants import InvariantViolation, assert_pairs_train_only, check_all
from src.train import (
    MAX_GRAD_NORM,
    Encoded,
    TokenBoundaryError,
    _latest_checkpoint,
    _pull_hub_checkpoints,
    _push,
    check_alignment,
    encode_episode,
    lr_lambda,
)

REFERENCE_FILE = "reference_logps.json"


# --- data --------------------------------------------------------------------


def load_pairs(
    data_dir: Path, cfg: ExperimentConfig, *, harvest_fingerprint: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """pairs.jsonl + manifest.json from make_pairs.py, refused unless they match."""
    suffix = ".smoke" if cfg.smoke else ""
    manifest_path = data_dir / f"manifest{suffix}.json"
    if not manifest_path.exists() and cfg.smoke:
        manifest_path = data_dir / "manifest.json"  # a smoke run may use the full set
    manifest = json.loads(manifest_path.read_text())
    expected = {
        "prompt_template_hash": cfg.prompt_template_hash,
        "system_prompt_hash": cfg.system_prompt_hash,
        "catalog_hash": cfg.eval.catalog_hashes.get(16),
        "teacher_fingerprint": harvest_fingerprint,
    }
    for key, want in expected.items():
        if manifest.get(key) != want:
            raise InvariantViolation(
                f"{manifest_path}: {key} is {str(manifest.get(key))[:12]}, config wants "
                f"{str(want)[:12]}. Rebuild the pairs with make_pairs.py."
            )
    if not manifest.get("executed"):
        raise InvariantViolation(
            f"{manifest_path}: built with --no-execute, so execution-equivalent calls may sit "
            "among the rejected samples. Rebuild with the equivalence check on."
        )
    data_path = data_dir / manifest["file"]
    if hash_file(data_path) != manifest["file_sha256"]:
        raise InvariantViolation(f"{data_path} does not match its manifest sha256")
    pairs = [json.loads(line) for line in data_path.read_text().splitlines() if line.strip()]
    return pairs, manifest


@dataclass(frozen=True)
class EncodedPair:
    pair_id: str
    chosen: Encoded
    rejected: Encoded

    @property
    def length(self) -> int:
        return max(len(self.chosen.input_ids), len(self.rejected.input_ids))


def encode_pair(tokenizer, pair: dict[str, Any]) -> EncodedPair:
    """Prompt + completion tokenized once each, loss mask on the completion only,
    with the same alignment check SFT runs (train.check_alignment)."""
    out = []
    for side in ("chosen", "rejected"):
        record = {
            "task_id": pair["task_id"],
            "rep": f"{pair['pair_id']}:{side}",
            "segments": [[pair["prompt"], False], [pair[side], True]],
        }
        enc = encode_episode(tokenizer, record)
        check_alignment(tokenizer, record, enc)
        out.append(enc)
    return EncodedPair(pair["pair_id"], out[0], out[1])


def encode_pairs(
    tokenizer, pairs: list[dict[str, Any]], max_len: int
) -> tuple[list[EncodedPair], dict[str, int]]:
    """Pairs whose tokens line up and fit; the rest are counted, never truncated."""
    kept: list[EncodedPair] = []
    skipped = {"token_boundary": 0, "too_long": 0}
    for p in pairs:
        try:
            e = encode_pair(tokenizer, p)
        except TokenBoundaryError:
            skipped["token_boundary"] += 1
            continue
        if e.length > max_len:
            skipped["too_long"] += 1
            continue
        kept.append(e)
    return kept, skipped


# --- the loss ----------------------------------------------------------------


def dpo_terms(
    beta: float, pc: float, pr: float, rc: float, rr: float
) -> tuple[float, float, float, float]:
    """(loss, coefficient on pc, coefficient on pr, z) for one pair. Pure, so the
    factorised gradient (module docstring, choice 3) is unit-tested on the Mac."""
    z = beta * ((pc - rc) - (pr - rr))
    loss = max(-z, 0.0) + math.log1p(math.exp(-abs(z)))  # -log sigmoid(z) = softplus(-z)
    s = 0.5 * (1.0 - math.tanh(z / 2))  # sigmoid(-z), no overflow at any z
    return loss, -beta * s, beta * s, z


def sequence_logp(model, enc: Encoded, device):
    """Summed log-prob of the labelled (completion) tokens: minus train.py's loss."""
    import torch

    from src.train import masked_lm_loss

    ids = torch.tensor([enc.input_ids], device=device)
    labels = torch.tensor([enc.labels], device=device)
    with torch.autocast(device.type, dtype=torch.float16, enabled=device.type == "cuda"):
        loss_sum, _ = masked_lm_loss(model, ids, labels)
    return -loss_sum


# --- model -------------------------------------------------------------------


def load_policy(cfg: ExperimentConfig, adapter_dir: Path, device, shard: bool):
    """The SFT adapter, trainable, on the fp16 base (choice 2)."""
    import torch
    from peft import PeftModel

    from src.train import load_base_model

    base = load_base_model(cfg, device, shard=shard)
    base.config.use_cache = False
    base.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    base.enable_input_require_grads()
    model = PeftModel.from_pretrained(base, str(adapter_dir), is_trainable=True)
    for p in model.parameters():
        if p.requires_grad:
            p.data = p.data.float()
    for m in model.modules():  # choice 3: the no-grad and grad passes must agree
        if isinstance(m, torch.nn.Dropout):
            m.p = 0.0
    return model


def reference_logps(
    model, pairs: list[EncodedPair], device, cache: Path, log
) -> dict[str, list[float]]:
    """The frozen reference: the starting adapter's log-probs, computed once."""
    import torch

    done: dict[str, list[float]] = json.loads(cache.read_text()) if cache.exists() else {}
    todo = [p for p in pairs if p.pair_id not in done]
    model.eval()
    with torch.no_grad():
        for i, p in enumerate(todo):
            done[p.pair_id] = [
                float(sequence_logp(model, p.chosen, device)),
                float(sequence_logp(model, p.rejected, device)),
            ]
            if (i + 1) % 50 == 0 or i + 1 == len(todo):
                cache.write_text(json.dumps(done))
                log(f"[train_dpo] reference log-probs {len(done)}/{len(pairs)}")
    model.train()
    return done


# --- training loop -----------------------------------------------------------


def train(
    cfg: ExperimentConfig,
    model,
    pairs: list[EncodedPair],
    ref: dict[str, list[float]],
    out_dir: Path,
    *,
    hub_repo: str | None = None,
    log=print,
) -> dict[str, Any]:
    import torch
    from peft import set_peft_model_state_dict
    from safetensors.torch import load_file

    assert cfg.dpo is not None
    dpo = cfg.dpo
    device = next(model.parameters()).device
    fp16 = device.type == "cuda"
    params = [p for p in model.parameters() if p.requires_grad]
    accum = dpo.batch_size * dpo.grad_accum
    steps_per_epoch = math.ceil(len(pairs) / accum)
    total_steps = dpo.max_steps or steps_per_epoch * dpo.epochs

    opt = torch.optim.AdamW(params, lr=dpo.learning_rate, weight_decay=0.0)
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lr_lambda(total_steps))
    scaler = torch.amp.GradScaler("cuda", enabled=fp16)  # pyright: ignore[reportPrivateImportUsage]

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
        log(f"[train_dpo] resumed from {ckpt.name} (step {step})")

    last: dict[str, float] = {}
    for epoch in range(start_epoch, dpo.epochs):
        if step >= total_steps:
            break
        order = random.Random(cfg.seed + epoch).sample(range(len(pairs)), len(pairs))
        for g in range(0, len(order), accum):
            group = [pairs[i] for i in order[g : g + accum]]
            losses, zs, margins_c, margins_r = [], [], [], []
            for p in group:
                rc, rr = ref[p.pair_id]
                with torch.no_grad():
                    pc = float(sequence_logp(model, p.chosen, device))
                    pr = float(sequence_logp(model, p.rejected, device))
                loss, coef_c, coef_r, z = dpo_terms(dpo.beta, pc, pr, rc, rr)
                for enc, coef in ((p.chosen, coef_c), (p.rejected, coef_r)):
                    scaler.scale(sequence_logp(model, enc, device) * (coef / len(group))).backward()
                losses.append(loss)
                zs.append(z)
                margins_c.append(dpo.beta * (pc - rc))
                margins_r.append(dpo.beta * (pr - rr))
            scaler.unscale_(opt)
            torch.nn.utils.clip_grad_norm_(params, MAX_GRAD_NORM)
            scaler.step(opt)
            scaler.update()
            opt.zero_grad(set_to_none=True)
            sched.step()
            step += 1
            n = len(group)
            last = {
                "loss": sum(losses) / n,
                "reward_accuracy": sum(z > 0 for z in zs) / n,
                "reward_margin": sum(zs) / n,
                "reward_chosen": sum(margins_c) / n,
                "reward_rejected": sum(margins_r) / n,
                "lr": sched.get_last_lr()[0],
                "epoch": epoch,
            }
            log(
                f"[train_dpo] epoch {epoch} step {step}/{total_steps} loss {last['loss']:.4f} "
                f"acc {last['reward_accuracy']:.2f} margin {last['reward_margin']:+.3f} "
                f"lr {last['lr']:.2e}",
                step=step,
                metrics=last,
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
        log(f"[train_dpo] saved {ckpt.name}")

    final = out_dir / "final"
    model.save_pretrained(final)
    return {"global_step": step, "total_steps": total_steps, "final": last, "final_dir": str(final)}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("DPO from the SFT adapter (plan X5, E3)")
    parser.add_argument("--data", type=Path, required=True, help="make_pairs.py output dir")
    parser.add_argument("--parent", required=True, help="the SFT adapter: Hub repo id or local dir")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--hub-repo", help="push checkpoints and the final adapter here")
    parser.add_argument("--wandb", action="store_true")
    parser.add_argument("--shard", action="store_true", help="3B on T4 x2, as train.py --shard")
    args = parser.parse_args(argv)

    cfg = resolve(args)
    print(f"[train_dpo] {', '.join(check_all(cfg))}")
    if cfg.dpo is None:
        parser.error(f"{args.config} sets no dpo section; use configs/v2/<model>-dpo.yaml")

    from src.config import load_config
    from src.registry import adapter_sha256, load_registry

    harvest_fp = load_config(args.config).teacher_fingerprint()
    raw_pairs, manifest = load_pairs(args.data, cfg, harvest_fingerprint=harvest_fp)
    # make_pairs.py checked the split against tau2 on the Mac; Kaggle has no tau2,
    # so every pair is re-checked against the train ids recorded there. The split
    # config itself is pinned by the teacher fingerprint load_pairs just matched.
    assert_pairs_train_only([p["task_id"] for p in raw_pairs], manifest["train_task_ids"])

    parent_dir = Path(args.parent)
    if not parent_dir.exists():
        from huggingface_hub import snapshot_download

        parent_dir = Path(snapshot_download(args.parent, allow_patterns=["adapter_*", "*.json"]))
    from src.invariants import assert_parent_is_sft

    parent_sha = adapter_sha256(parent_dir)
    parent_repo = assert_parent_is_sft(parent_sha, load_registry())
    print(f"[train_dpo] parent {parent_repo} ({parent_sha[:12]}) is a registered SFT checkpoint")
    if manifest.get("adapter") != parent_repo:
        raise InvariantViolation(
            f"the pairs were built from {manifest.get('adapter')}'s completions, not {parent_repo}'s"
        )

    import torch
    import transformers
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(cfg.model.base_model)
    pairs, skipped = encode_pairs(tokenizer, raw_pairs, cfg.train.max_seq_len)
    if cfg.smoke:
        # The longest pair, not the first: one step then proves T4 memory, as SFT's smoke does.
        pairs = sorted(pairs, key=lambda p: -p.length)[: cfg.dpo.batch_size * cfg.dpo.grad_accum]
    if not pairs:
        raise InvariantViolation(f"no usable pairs (skipped: {skipped})")
    lengths = sorted(p.length for p in pairs)
    print(
        f"[train_dpo] {len(pairs)} pairs (skipped {skipped}), lengths p50 "
        f"{lengths[len(lengths) // 2]} max {lengths[-1]}; alignment checked"
    )

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = load_policy(cfg, parent_dir, device, args.shard)
    model.print_trainable_parameters()

    run = None
    if args.wandb:
        import wandb

        run = wandb.init(  # pyright: ignore[reportAttributeAccessIssue]
            project=os.environ.get("WANDB_PROJECT", "tool-router-ladder"),
            name=cfg.name,
            config={
                **cfg.model_dump(mode="json"),
                "pairs_manifest": manifest,
                "parent": parent_repo,
            },
        )

    def log(msg, step=None, metrics=None):
        print(msg, flush=True)
        if run is not None and metrics:
            run.log(metrics, step=step)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    ref = reference_logps(model, pairs, device, args.output_dir / REFERENCE_FILE, log)
    result = train(cfg, model, pairs, ref, args.output_dir, hub_repo=args.hub_repo, log=log)

    summary = {
        **result,
        "config": cfg.name,
        "config_fingerprint": cfg.fingerprint(),
        "method": cfg.method(),
        "dpo": cfg.dpo.model_dump(mode="json"),
        "base_model": cfg.model.base_model,
        "parent_adapter": parent_repo,
        "parent_sha256": parent_sha,
        "smoke": cfg.smoke,
        "pairs": {
            k: manifest.get(k)
            for k in (
                "file_sha256",
                "n_pairs",
                "pairs_per_bucket",
                "completions_sha256",
                "sampling",
            )
        },
        "n_pairs_used": len(pairs),
        "skipped": skipped,
        "seconds": round(time.time() - started, 1),
        "versions": {"torch": torch.__version__, "transformers": transformers.__version__},
        "hub_repo": args.hub_repo,
        "sharded": args.shard,
    }
    final = Path(result["final_dir"])
    (final / "run.json").write_text(json.dumps(summary, indent=2) + "\n")
    _push(args.hub_repo, final, ".")
    print(f"[train_dpo] done: {json.dumps(summary)}")
    if run is not None:
        run.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
