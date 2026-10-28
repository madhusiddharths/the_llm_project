"""Stage G2: step-level GRPO from the SFT adapter (plan X7). Runs on Kaggle.

For each train state, sample a group of completions from the policy, score each
with src/reward.py (execution on a replayed tau2 retail DB, no API), and push
the policy toward the completions that beat their group's mean.

Choices that differ from the plan's sketch, and why (review these):
  1. NOT TRL's GRPOTrainer, for train_dpo.py's reason: prompts reach ~12k tokens
     and a T4 holds one sequence's activations at a time. This loop reuses
     train_dpo's machinery (fp16 base, fp32 LoRA, logits at the completion
     positions only, gradient checkpointing, one sequence per backward).
  2. Policy = the SFT LoRA trained further (DPO's choice 2). Reference = that
     adapter's weights at step 0, kept as a copy and swapped in for the no-grad
     reference pass: an exact SFT reference at the cost of one LoRA's memory.
  3. One gradient step per sampled batch (GRPO with mu = 1), so the importance
     ratio is 1 and the clipped objective reduces to
         L_i = -A_i * mean_t log pi(o_i,t) + kl_coef * mean_t k3_t
         k3_t = exp(ref_t - pi_t) - (ref_t - pi_t) - 1
     with A_i = (r_i - mean(r)) / (std(r) + eps) inside the group, averaged
     over the step's completions. Each completion backpropagates on its own.
  4. A group whose rewards are all equal has no advantage and is skipped (no
     gradient, not even KL); the share of such groups is logged, since a
     policy that already gets every state right, or wrong, learns nothing.
  5. Completions are trained on as sampled token ids (prompt ids + generated
     ids up to and including the stop token), never re-tokenised text, so the
     tokens scored are the tokens generated. A completion cut at max_new_tokens
     keeps its cut tokens and is scored as whatever it parses to.
  6. Sampling is seeded per (step, state), so a resumed run samples the same
     batches it would have.

States come from `make_pairs.py states` (data/grpo/: the SFT set's 1,099
train-split decisions with the teacher's action and the calls to replay),
uploaded to Kaggle as a dataset, because Kaggle has neither the harvest
trajectories nor the split recomputation.

    python src/train_grpo.py --config configs/v2/qwen05b-grpo.yaml --data <states dir> \\
        --parent madhusiddharths1/tool-router-qwen05b-sft \\
        --output-dir /kaggle/working/qwen05b-grpo --hub-repo <user>/tool-router-qwen05b-grpo --wandb
    # smoke: one step, one state (the longest prompt), 2 samples of 32 tokens, no Hub push
    python src/train_grpo.py ... --smoke
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cli import build_parser, resolve
from src.config import ExperimentConfig
from src.hashing import hash_file
from src.invariants import (
    InvariantViolation,
    assert_reward_deterministic,
    assert_states_train_only,
    check_all,
)
from src.make_pairs import NATIVE_CATALOG, Runner
from src.prompts import IM_END, render_action
from src.reward import Reward, step_reward
from src.train import IGNORE, MAX_GRAD_NORM, Encoded, _push, lr_lambda
from src.trajectories import Action, ToolCall

ADV_EPS = 1e-4
STOP_TOKENS = (IM_END, "<|endoftext|>")


# --- data --------------------------------------------------------------------


@dataclass(frozen=True)
class GState:
    step_id: str
    task_id: str
    prompt: str
    reference: Action
    context: tuple[dict[str, Any], ...]  # only what reward's replay reads: prior calls


def _calls(raw: Sequence[dict[str, Any]]) -> tuple[ToolCall, ...]:
    return tuple(ToolCall(name=c["name"], arguments=dict(c["arguments"])) for c in raw)


def to_state(row: dict[str, Any]) -> GState:
    ref = row["reference"]
    reference = (
        Action(kind="reply", text=ref["text"])
        if ref["kind"] == "reply"
        else Action(kind="tool_call", calls=_calls(ref["calls"]))
    )
    context = tuple(
        {"role": "assistant", "tool_calls": [{"name": c["name"], "arguments": c["arguments"]}]}
        for c in row["prior_calls"]
    )
    return GState(row["step_id"], row["task_id"], row["prompt"], reference, context)


def load_states_dir(
    data_dir: Path, cfg: ExperimentConfig, *, harvest_fingerprint: str
) -> tuple[list[GState], dict[str, Any]]:
    """states.jsonl + manifest.json from `make_pairs.py states`, refused unless they match."""
    manifest = json.loads((data_dir / "manifest.json").read_text())
    expected = {
        "prompt_template_hash": cfg.prompt_template_hash,
        "system_prompt_hash": cfg.system_prompt_hash,
        "catalog_hash": cfg.eval.catalog_hashes.get(NATIVE_CATALOG),
        "teacher_fingerprint": harvest_fingerprint,
    }
    for key, want in expected.items():
        if manifest.get(key) != want:
            raise InvariantViolation(
                f"{data_dir}/manifest.json: {key} is {str(manifest.get(key))[:12]}, config wants "
                f"{str(want)[:12]}. Re-export with `make_pairs.py states`."
            )
    path = data_dir / manifest["file"]
    if hash_file(path) != manifest["file_sha256"]:
        raise InvariantViolation(f"{path} does not match its manifest sha256")
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    states = [to_state(r) for r in rows]
    assert_states_train_only([s.task_id for s in states], manifest["train_task_ids"])
    return states, manifest


def batch_for(step: int, n_states: int, per_step: int, seed: int) -> list[int]:
    """State indices for one step: seeded passes over a shuffled order (choice 6)."""
    out = []
    for slot in range(step * per_step, (step + 1) * per_step):
        epoch, pos = divmod(slot, n_states)
        out.append(random.Random(seed + epoch).sample(range(n_states), n_states)[pos])
    return out


# --- the loss ----------------------------------------------------------------


def group_advantages(rewards: Sequence[float]) -> list[float] | None:
    """(r - mean) / (std + eps) in the group; None when every reward is equal."""
    mean = sum(rewards) / len(rewards)
    std = math.sqrt(sum((r - mean) ** 2 for r in rewards) / len(rewards))
    if std < 1e-6:
        return None
    return [(r - mean) / (std + ADV_EPS) for r in rewards]


def token_logps(model, enc: Encoded, device):
    """Per-token log-probs of the labelled (completion) tokens, with grad."""
    import torch

    ids = torch.tensor([enc.input_ids], device=device)
    labels = torch.tensor([enc.labels], device=device)
    with torch.autocast(device.type, dtype=torch.float16, enabled=device.type == "cuda"):
        base = model.get_base_model()
        hidden = base.model(input_ids=ids).last_hidden_state
        targets = labels[:, 1:].to(hidden.device)
        mask = targets != IGNORE
        logits = base.lm_head(hidden[:, :-1][mask]).float()
    return torch.log_softmax(logits, -1).gather(-1, targets[mask].unsqueeze(-1)).squeeze(-1)


def completion_loss(pol, ref, advantage: float, kl_coef: float):
    """Choice 3 for one completion: (loss, mean k3) from per-token log-probs."""
    diff = ref - pol
    k3 = diff.exp() - diff - 1
    return -advantage * pol.mean() + kl_coef * k3.mean(), k3.mean()


@contextmanager
def swapped(params: Sequence[Any], values: Sequence[Any]):
    """Temporarily load `values` into `params` (the reference pass, choice 2)."""
    import torch

    saved = [p.detach().clone() for p in params]
    with torch.no_grad():
        for p, v in zip(params, values, strict=True):
            p.copy_(v)
    try:
        yield
    finally:
        with torch.no_grad():
            for p, v in zip(params, saved, strict=True):
                p.copy_(v)


# --- sampling ----------------------------------------------------------------


def sample_group(model, tokenizer, state: GState, cfg: ExperimentConfig, device, seed: int):
    """[(prompt ids, generated ids, text)] for cfg.grpo.group_size samples."""
    import torch

    assert cfg.grpo is not None
    g = cfg.grpo
    stops = [tokenizer.convert_tokens_to_ids(t) for t in STOP_TOKENS]
    prompt_ids = tokenizer(state.prompt, add_special_tokens=False).input_ids
    ids = torch.tensor([prompt_ids], device=device)
    torch.manual_seed(seed)
    was_training = model.training
    model.eval()
    with (
        torch.no_grad(),
        torch.autocast(device.type, dtype=torch.float16, enabled=device.type == "cuda"),
    ):
        out = model.generate(
            input_ids=ids,
            attention_mask=torch.ones_like(ids),
            do_sample=True,
            temperature=g.temperature,
            top_p=1.0,
            top_k=0,
            max_new_tokens=g.max_new_tokens,
            num_return_sequences=g.group_size,
            eos_token_id=stops,
            pad_token_id=stops[-1],
            use_cache=True,
        )
    model.train(was_training)
    samples = []
    for row in out[:, len(prompt_ids) :].tolist():
        gen = []
        for t in row:
            gen.append(t)
            if t in stops:
                break
        samples.append((prompt_ids, gen, tokenizer.decode(gen, skip_special_tokens=False)))
    return samples


# --- training loop -----------------------------------------------------------


def _latest_step_checkpoint(out_dir: Path) -> Path | None:
    ckpts = sorted(out_dir.glob("checkpoint-step-*"), key=lambda p: int(p.name.rsplit("-", 1)[1]))
    return ckpts[-1] if ckpts else None


def train(
    cfg: ExperimentConfig,
    model,
    tokenizer,
    states: list[GState],
    tools: Sequence[dict[str, Any]],
    run: Runner,
    out_dir: Path,
    *,
    hub_repo: str | None = None,
    log: Callable[..., None] = print,
    sampler=sample_group,
    backward_on_ties: bool = False,
) -> dict[str, Any]:
    """`backward_on_ties` (the smoke's memory check): a group with equal rewards
    still runs its forward and backward, with advantage 0. Its only gradient is
    the KL term's, zero at the reference, so the adapter does not move; without
    it a tied smoke group (2026-10-28: both samples replies at a reply state)
    skips the backward and the check passes without measuring anything."""
    import torch
    from peft import set_peft_model_state_dict
    from safetensors.torch import load_file

    assert cfg.grpo is not None
    g = cfg.grpo
    device = next(model.parameters()).device
    fp16 = device.type == "cuda"
    params = [p for p in model.parameters() if p.requires_grad]
    reference = [p.detach().clone() for p in params]  # choice 2: before any resume
    opt = torch.optim.AdamW(params, lr=g.learning_rate, weight_decay=0.0)
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lr_lambda(g.max_steps))
    scaler = torch.amp.GradScaler("cuda", enabled=fp16)  # pyright: ignore[reportPrivateImportUsage]

    step = 0
    if hub_repo:
        _pull_hub_checkpoints_steps(hub_repo, out_dir)
    if (ckpt := _latest_step_checkpoint(out_dir)) is not None:
        state = torch.load(ckpt / "trainer_state.pt", map_location=device, weights_only=False)
        if state.get("config_fingerprint") != cfg.fingerprint():
            raise InvariantViolation(
                f"{ckpt.name} was saved by another config "
                f"({str(state.get('config_fingerprint'))[:12]}, this is {cfg.fingerprint()[:12]}); "
                "use a fresh --output-dir and --hub-repo"
            )
        set_peft_model_state_dict(model, load_file(str(ckpt / "adapter_model.safetensors")))
        opt.load_state_dict(state["optimizer"])
        sched.load_state_dict(state["scheduler"])
        scaler.load_state_dict(state["scaler"])
        step = state["global_step"]
        log(f"[train_grpo] resumed from {ckpt.name} (step {step})")

    history: list[float] = []
    last: dict[str, float] = {}
    while step < g.max_steps:
        batch = [states[i] for i in batch_for(step, len(states), g.prompts_per_step, cfg.seed)]
        groups, rewards_all, comps = [], [], defaultdict(list)
        lengths, truncated, calls = [], 0, 0
        for j, st in enumerate(batch):
            samples = sampler(model, tokenizer, st, cfg, device, seed=cfg.seed + step * 1009 + j)
            rewards: list[Reward] = [
                step_reward(st.reference, text, st.context, tools, run, g.reward_weights)
                for _, _, text in samples
            ]
            for (_, gen, text), r in zip(samples, rewards, strict=True):
                rewards_all.append(r.total)
                for k, v in r.components.items():
                    comps[k].append(v)
                lengths.append(len(gen))
                truncated += not any(text.endswith(t) for t in STOP_TOKENS)
                calls += "<tool_call>" in text
            adv = group_advantages([r.total for r in rewards])
            groups.append((st, samples, adv))

        active = [
            (st, s, a)
            for st, ss, adv in groups
            if adv is not None or backward_on_ties
            for s, a in zip(ss, adv or [0.0] * len(ss), strict=True)
        ]
        kls = []
        for st, (prompt_ids, gen, _), a in active:
            enc = Encoded(st.step_id, prompt_ids + gen, [IGNORE] * len(prompt_ids) + gen)
            with torch.no_grad(), swapped(params, reference):
                ref = token_logps(model, enc, device)
            pol = token_logps(model, enc, device)
            loss, kl = completion_loss(pol, ref.to(pol.device), a, g.kl_coef)
            scaler.scale(loss / len(active)).backward()
            kls.append(float(kl))
        if active:
            scaler.unscale_(opt)
            torch.nn.utils.clip_grad_norm_(params, MAX_GRAD_NORM)
            scaler.step(opt)
            scaler.update()
        opt.zero_grad(set_to_none=True)
        sched.step()
        step += 1

        n = len(rewards_all)
        history.append(sum(rewards_all) / n)
        last = {
            "reward": history[-1],
            "zero_std_groups": sum(adv is None for *_, adv in groups) / len(groups),
            "kl": sum(kls) / len(kls) if kls else 0.0,
            "backward_seqs": len(active),
            "call_rate": calls / n,
            "completion_tokens": sum(lengths) / n,
            "truncated": truncated / n,
            "lr": sched.get_last_lr()[0],
            **{f"r_{k}": sum(v) / len(v) for k, v in comps.items()},
        }
        log(
            f"[train_grpo] step {step}/{g.max_steps} reward {last['reward']:.3f} "
            f"execution {last.get('r_execution', 0.0):.3f} kl {last['kl']:.4f} "
            f"zero-std {last['zero_std_groups']:.2f} calls {last['call_rate']:.2f} "
            f"len {last['completion_tokens']:.0f} backward {len(active)}",
            step=step,
            metrics=last,
        )
        if step % g.save_every == 0 or step == g.max_steps:
            ckpt = out_dir / f"checkpoint-step-{step}"
            model.save_pretrained(ckpt)
            torch.save(
                {
                    "optimizer": opt.state_dict(),
                    "scheduler": sched.state_dict(),
                    "scaler": scaler.state_dict(),
                    "global_step": step,
                    "config_fingerprint": cfg.fingerprint(),
                },
                ckpt / "trainer_state.pt",
            )
            _push(hub_repo, ckpt, f"checkpoints/step-{step}")
            log(f"[train_grpo] saved {ckpt.name}")

    final = out_dir / "final"
    model.save_pretrained(final)
    window = max(1, min(25, len(history) // 4))
    return {
        "global_step": step,
        "max_steps": g.max_steps,
        "final": last,
        "reward_first": sum(history[:window]) / window if history else None,
        "reward_last": sum(history[-window:]) / window if history else None,
        "final_dir": str(final),
    }


def _pull_hub_checkpoints_steps(repo: str, out_dir: Path) -> None:
    """train._pull_hub_checkpoints, for step-numbered checkpoints."""
    import shutil

    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import RepositoryNotFoundError

    try:
        snap = Path(snapshot_download(repo, allow_patterns=["checkpoints/*"]))
    except RepositoryNotFoundError:
        return
    for ckpt in sorted((snap / "checkpoints").glob("step-*")):
        dest = out_dir / f"checkpoint-{ckpt.name}"
        if not dest.exists():
            shutil.copytree(ckpt, dest)


# --- CLI ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("Step-level GRPO from the SFT adapter (plan X7, G2)")
    parser.add_argument("--data", type=Path, required=True, help="make_pairs.py states output dir")
    parser.add_argument("--parent", required=True, help="the SFT adapter: Hub repo id or local dir")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--hub-repo", help="push checkpoints and the final adapter here")
    parser.add_argument("--wandb", action="store_true")
    parser.add_argument("--shard", action="store_true", help="3B on T4 x2, as train.py --shard")
    args = parser.parse_args(argv)

    cfg = resolve(args)
    print(f"[train_grpo] {', '.join(check_all(cfg))}")
    if cfg.grpo is None or cfg.grpo.level != "step":
        parser.error(
            f"{args.config} sets no step-level grpo section; use configs/v2/<model>-grpo.yaml"
        )

    from src.catalogs import load_catalog
    from src.config import load_config
    from src.make_pairs import Tau2Runner
    from src.registry import adapter_sha256, load_registry

    harvest_fp = load_config(args.config).teacher_fingerprint()
    states, manifest = load_states_dir(args.data, cfg, harvest_fingerprint=harvest_fp)
    if cfg.smoke:
        states = sorted(states, key=lambda s: -len(s.prompt))[:1]  # memory check, as DPO's smoke
    tools = load_catalog(cfg, NATIVE_CATALOG)
    run = Tau2Runner()

    # The reward must be deterministic (plan X10), and the teacher's own action
    # must score 1 at its own state, or the reward measures something else.
    weights = cfg.grpo.reward_weights
    probe = states[:8]
    teacher = [(s, render_action(s.reference) + IM_END) for s in probe]

    def reward_of(sample: tuple[GState, str]) -> float:
        st, text = sample
        return step_reward(st.reference, text, st.context, tools, run, weights).total

    assert_reward_deterministic(reward_of, teacher)
    if any(reward_of(t) != 1.0 for t in teacher):
        raise InvariantViolation("the teacher's own action does not score 1 at its state")
    print(
        f"[train_grpo] {len(states)} states ({manifest['n_reply_states']} reply states in the export); reward checked"
    )

    parent_dir = Path(args.parent)
    if not parent_dir.exists():
        from huggingface_hub import snapshot_download

        parent_dir = Path(snapshot_download(args.parent, allow_patterns=["adapter_*", "*.json"]))
    from src.invariants import assert_parent_is_sft

    parent_sha = adapter_sha256(parent_dir)
    parent_repo = assert_parent_is_sft(parent_sha, load_registry())
    print(f"[train_grpo] parent {parent_repo} ({parent_sha[:12]}) is a registered SFT checkpoint")

    import torch
    import transformers
    from transformers import AutoTokenizer

    from src.train_dpo import load_policy

    tokenizer = AutoTokenizer.from_pretrained(cfg.model.base_model)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = load_policy(cfg, parent_dir, device, args.shard)
    model.print_trainable_parameters()

    wb = None
    if args.wandb:
        import wandb

        wb = wandb.init(  # pyright: ignore[reportAttributeAccessIssue]
            project=os.environ.get("WANDB_PROJECT", "tool-router-ladder"),
            name=cfg.name,
            config={
                **cfg.model_dump(mode="json"),
                "states_manifest": manifest,
                "parent": parent_repo,
            },
        )

    def log(msg, step=None, metrics=None):
        print(msg, flush=True)
        if wb is not None and metrics:
            wb.log(metrics, step=step)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    result = train(
        cfg,
        model,
        tokenizer,
        states,
        tools,
        run,
        args.output_dir,
        hub_repo=args.hub_repo,
        log=log,
        backward_on_ties=cfg.smoke,
    )
    summary = {
        **result,
        "config": cfg.name,
        "config_fingerprint": cfg.fingerprint(),
        "method": cfg.method(),
        "grpo": cfg.grpo.model_dump(mode="json"),
        "base_model": cfg.model.base_model,
        "parent_adapter": parent_repo,
        "parent_sha256": parent_sha,
        "smoke": cfg.smoke,
        "states": {k: manifest.get(k) for k in ("file_sha256", "n_states", "n_reply_states")},
        "seconds": round(time.time() - started, 1),
        "versions": {"torch": torch.__version__, "transformers": transformers.__version__},
        "hub_repo": args.hub_repo,
        "sharded": args.shard,
    }
    final = Path(result["final_dir"])
    (final / "run.json").write_text(json.dumps(summary, indent=2) + "\n")
    _push(args.hub_repo, final, ".")
    print(f"[train_grpo] done: {json.dumps(summary)}")
    if wb is not None:
        wb.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
