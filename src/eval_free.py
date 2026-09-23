"""Free-running end-to-end evaluation over tau2-bench retail (plan §4, V1-12).

The student drives. Its own mistakes propagate, the frozen user simulator
reacts to them, and tau2 scores the final database state. This is the
task-success half of every headline number; eval_forced.py is the per-step half.

How the student plugs into tau2
-------------------------------
A tau2 agent factory ("student_c<N>") builds StudentAgent, a subclass of tau2's
own LLMAgent that replaces exactly one thing: how the next message is produced.
Everything else (the orchestrator, the environment, the user simulator, the
NL-assertion judge, the scoring) is the code the teacher baseline ran through.
Each turn the agent:
  1. converts tau2's message history into plain dicts (greeting first: tau2
     seeds the agent's history with it, exactly as in the harvest);
  2. renders the prompt with src/prompts.serialize_state, the same function
     training and teacher-forced eval use, over the pinned system prompt and the
     pinned catalog (16, 40 or 80 tools);
  3. asks a policy (vLLM, HF, or a fixed string for plumbing checks) for a
     completion, and parses it with src/prompts.parse_completion;
  4. returns a tau2 AssistantMessage: the tool call(s), or the reply text.
The raw completion and any parse error ride along in AssistantMessage.raw_data,
so every saved trajectory can be re-audited step by step.

Harness rules that are choices, not tau2 behaviour (review these):
  - An unparseable completion is sent to the user as plain text, as a deployed
    agent without a retry loop would do. It is counted, never hidden.
  - An empty completion becomes the reply "[empty response]" (tau2 refuses an
    assistant message with neither text nor a tool call).
  - A prompt longer than the model's context gets the reply "I'm sorry, I can't
    continue this conversation." and is flagged context_overflow. Raising would
    make tau2 mark the episode an infrastructure error, which this runner
    leaves unlogged and retries forever.
Distractor calls reach the environment and come back as tau2's "not found"
error, exactly as a hallucinated name does.

Guards before any quota is spent:
  - The harness is the baseline's: cfg.teacher_fingerprint() (teacher, split,
    simulator, judge) must equal the fingerprint recorded in
    results/harvest-baseline.jsonl. Stage A students face the same user.
  - tau2 on this machine renders the same system prompt and native tool
    schemas as the pinned snapshot (data/catalogs/). A different tau2 checkout
    fails here, not three days into Stage A. Pinned tau2: 672227c6 (Kaggle:
    clone sierra-research/tau2-bench at that commit, pip install -e .).

Kaggle sessions start on an empty disk, so resuming Stage A across its ~10
daily sessions (plan §7 constraint 1) needs the log off the machine. With
--hub-sync <user>/<dataset-repo>, the run pulls this cell's results file and
trajectories from a private HF dataset repo before starting, and pushes the
results file and each new trajectory batch after every batch. Paths in the
repo mirror the local ones, so `huggingface-cli download --repo-type dataset`
on the Mac lands them exactly where the scoring scripts look.

Never run this on the same UTC day as a local harvest: both draw on one
account-wide 1,000/day and 20 RPM budget, and each machine's throttle only
counts its own requests.

Pacing, budgeting and failure handling are harvest.py's: the same throttle
(simulator + judge calls are the only API requests; the student is local), the
same affordable_episodes guard, the same stop-after-two-empty-batches breaker,
and failed episodes stay unlogged so the next run retries them.

    # on Kaggle, after the vLLM + tau2 setup cells
    python src/eval_free.py --config configs/qwen15b.yaml --catalog 16 \\
        --adapter <hf-user>/tool-router-qwen15b-sft
    # smoke: 2 tasks, 1 seed, 2 simulator turns (a few API requests)
    python src/eval_free.py --config configs/qwen05b.yaml --catalog 16 --smoke
    # plumbing check with no GPU: every student turn is the same fixed reply
    python src/eval_free.py --config configs/qwen05b.yaml --backend fixed --smoke
"""

from __future__ import annotations

import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# tau2 logs its whole registry at import time; quiet it before anything imports it.
try:
    from loguru import logger as _loguru

    _loguru.remove()
    _loguru.add(sys.stderr, level="WARNING")
except ImportError:
    pass

from src.catalogs import load_catalog, load_system_prompt
from src.cli import build_parser, resolve
from src.hashing import hash_obj, short
from src.invariants import InvariantViolation, check_all
from src.prompts import Parsed, parse_completion, serialize_state, template_hash
from src.trajectories import DEGENERATE_USER_CHARS

EMPTY_REPLY = "[empty response]"
OVERFLOW_REPLY = "I'm sorry, I can't continue this conversation."
FIXED_REPLY = "Could you please tell me your email address?"
TAU2_COMMIT = "672227c6b6676edc20d57ea53b7000262aae77b9"


# --- pure pieces (tested without tau2 or a GPU) --------------------------------


def history_dicts(messages: Sequence[Any]) -> list[dict[str, Any]]:
    """tau2 message objects -> the dict shape src/prompts renders."""
    out = []
    for m in messages:
        calls = (
            [{"name": c.name, "arguments": dict(c.arguments)} for c in (m.tool_calls or [])]
            if getattr(m, "tool_calls", None)
            else None
        )
        out.append({"role": m.role, "content": m.content, "tool_calls": calls})
    return out


@dataclass(frozen=True)
class Turn:
    """What the student said this turn, before it becomes a tau2 message."""

    content: str | None
    tool_calls: list[dict[str, Any]]  # [{"id", "name", "arguments"}]
    raw: dict[str, Any]  # audit trail stored in AssistantMessage.raw_data


def decide_turn(completion: str | None, *, overflow: bool = False) -> Turn:
    """A completion -> the message tau2 will see. See the harness rules above."""
    if overflow:
        return Turn(OVERFLOW_REPLY, [], {"completion": None, "context_overflow": True})
    parsed: Parsed = parse_completion(completion or "")
    raw = {"completion": completion, "parse_error": parsed.error, "stray_text": parsed.stray_text}
    if parsed.action is not None and parsed.action.kind == "tool_call":
        calls = [
            {"id": f"call_{uuid.uuid4().hex[:24]}", "name": c.name, "arguments": dict(c.arguments)}
            for c in parsed.action.calls
        ]
        return Turn(None, calls, raw)
    if parsed.action is not None:
        return Turn(parsed.action.text, [], raw)
    text = (completion or "").split("<|im_end|>", 1)[0].strip()
    return Turn(text or EMPTY_REPLY, [], raw)


def episode_stats(
    messages: Sequence[Mapping[str, Any]], catalog_names: set[str], native_names: set[str]
) -> dict[str, Any]:
    """Per-episode diagnostics from a saved tau2 trajectory (dicts)."""
    student = [m for m in messages[1:] if m.get("role") == "assistant"]  # [0] is the greeting
    raws = [m.get("raw_data") or {} for m in student]
    calls = [c["name"] for m in student for c in (m.get("tool_calls") or [])]
    user_lengths = [len(m.get("content") or "") for m in messages if m.get("role") == "user"]
    return {
        "student_turns": len(student),
        "tool_calls": len(calls),
        "parse_errors": sum(1 for r in raws if r.get("parse_error")),
        "context_overflows": sum(1 for r in raws if r.get("context_overflow")),
        "distractor_calls": sum(1 for n in calls if n in catalog_names and n not in native_names),
        "hallucinated_calls": sum(1 for n in calls if n not in catalog_names),
        "degenerate_user_turn": any(n > DEGENERATE_USER_CHARS for n in user_lengths),
    }


# --- policies ----------------------------------------------------------------


class FixedPolicy:
    """Every turn is the same reply. Plumbing checks only: never a result."""

    name = "fixed"

    def __init__(self, text: str = FIXED_REPLY):
        self.text = text

    def fits(self, prompt: str) -> bool:
        return True

    def __call__(self, prompt: str) -> str:
        return self.text


class VLLMPolicy:
    """One in-process vLLM engine, greedy, the same settings as generate.py."""

    name = "vllm"

    def __init__(self, cfg, adapter: str | None, max_model_len: int):
        from vllm import LLM, SamplingParams

        from src.generate import _resolve_adapter
        from src.prompts import IM_END

        path = _resolve_adapter(adapter)
        self.llm = LLM(
            model=cfg.model.base_model,
            dtype="half",
            max_model_len=max_model_len,
            gpu_memory_utilization=0.9,
            seed=cfg.seed,
            enable_lora=path is not None,
            max_lora_rank=cfg.train.lora_r,
        )
        self.params = SamplingParams(
            temperature=0.0, max_tokens=cfg.teacher.max_tokens, stop=[IM_END], seed=cfg.seed
        )
        self.lora = None
        if path is not None:
            from vllm.lora.request import LoRARequest

            self.lora = LoRARequest("student", 1, path)
        self.tokenizer = self.llm.get_tokenizer()
        self.limit = max_model_len - cfg.teacher.max_tokens

    def fits(self, prompt: str) -> bool:
        return len(self.tokenizer(prompt, add_special_tokens=False)["input_ids"]) <= self.limit

    def __call__(self, prompt: str) -> str:
        out = self.llm.generate([prompt], self.params, lora_request=self.lora, use_tqdm=False)
        return out[0].outputs[0].text


class HFPolicy:
    """transformers generate(): the fallback if vLLM breaks (plan §10)."""

    name = "hf"

    def __init__(self, cfg, adapter: str | None, max_model_len: int):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        from src.prompts import IM_END

        self.tok = AutoTokenizer.from_pretrained(cfg.model.base_model)
        model = AutoModelForCausalLM.from_pretrained(
            cfg.model.base_model, torch_dtype=torch.float16
        )
        if adapter is not None:
            from peft import PeftModel

            model = PeftModel.from_pretrained(model, adapter)
        self.model = model.to("cuda").eval()
        self.stop = self.tok.convert_tokens_to_ids(IM_END)
        self.max_new = cfg.teacher.max_tokens
        self.limit = max_model_len - self.max_new

    def fits(self, prompt: str) -> bool:
        return len(self.tok(prompt, add_special_tokens=False).input_ids) <= self.limit

    def __call__(self, prompt: str) -> str:
        import torch

        ids = self.tok(prompt, return_tensors="pt", add_special_tokens=False).input_ids.to("cuda")
        with torch.no_grad():
            out = self.model.generate(
                ids, do_sample=False, max_new_tokens=self.max_new, eos_token_id=self.stop
            )
        return self.tok.decode(out[0, ids.shape[1] :], skip_special_tokens=False)


# --- Hub persistence (Kaggle sessions start empty) -------------------------------


def hub_pull(repo: str, results_file: Path, traj_dir: Path) -> None:
    """Restore this cell's results and trajectories, unless already on disk."""
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import RepositoryNotFoundError

    if results_file.exists():
        print(f"[eval_free] {results_file} exists locally; not pulling it from {repo}")
        return
    try:
        snapshot_download(
            repo_id=repo,
            repo_type="dataset",
            allow_patterns=[results_file.as_posix(), f"{traj_dir.as_posix()}/*"],
            local_dir=".",
        )
    except RepositoryNotFoundError:
        print(f"[eval_free] {repo} does not exist yet; starting fresh")


def hub_push(repo: str, files: Sequence[Path]) -> None:
    from huggingface_hub import HfApi

    api = HfApi()
    api.create_repo(repo, repo_type="dataset", private=True, exist_ok=True)
    for f in files:
        api.upload_file(
            path_or_fileobj=str(f), path_in_repo=f.as_posix(), repo_id=repo, repo_type="dataset"
        )


# --- tau2 wiring ---------------------------------------------------------------


def check_tau2_matches_snapshot(tools, domain_policy: str, system_text: str, native: list) -> None:
    """The tau2 on this machine must be the one the snapshot came from."""
    from tau2.agent.llm_agent import AGENT_INSTRUCTION, SYSTEM_PROMPT

    live = SYSTEM_PROMPT.format(domain_policy=domain_policy, agent_instruction=AGENT_INSTRUCTION)
    if live != system_text:
        raise InvariantViolation(
            "tau2's live system prompt differs from data/catalogs/system_prompt.txt; "
            f"install tau2-bench at {TAU2_COMMIT[:8]}"
        )
    live_schemas = {t.name: t.openai_schema for t in tools}
    pinned = {t["function"]["name"]: t for t in native}
    if live_schemas != pinned:
        raise InvariantViolation(
            "tau2's live retail tool schemas differ from data/catalogs/catalog-16.json; "
            f"install tau2-bench at {TAU2_COMMIT[:8]}"
        )


def register_student_agent(
    size: int, catalog: list, native: list, system_text: str, policy: Callable[[str], str]
) -> str:
    """Register tau2 agent factory 'student_c<size>' and return its name."""
    from tau2.agent.llm_agent import LLMAgent
    from tau2.data_model.message import AssistantMessage, MultiToolMessage, ToolCall
    from tau2.registry import registry

    class StudentAgent(LLMAgent):
        def _generate_next_message(self, message, state):
            if isinstance(message, MultiToolMessage):
                state.messages.extend(message.tool_messages)
            else:
                state.messages.append(message)
            prompt = serialize_state(
                system=system_text, tools=catalog, messages=history_dicts(state.messages)
            )
            started = time.time()
            if policy.fits(prompt):
                turn = decide_turn(policy(prompt))
            else:
                turn = decide_turn(None, overflow=True)
            raw = {**turn.raw, "generation_seconds": round(time.time() - started, 3)}
            return AssistantMessage(
                role="assistant",
                content=turn.content,
                tool_calls=[
                    ToolCall(
                        id=c["id"], name=c["name"], arguments=c["arguments"], requestor="assistant"
                    )
                    for c in turn.tool_calls
                ]
                or None,
                raw_data=raw,
            )

    def factory(tools, domain_policy, **kwargs):
        check_tau2_matches_snapshot(tools, domain_policy, system_text, native)
        return StudentAgent(
            tools=tools,
            domain_policy=domain_policy,
            llm=f"student:{policy.name}",
            llm_args={},
        )

    name = f"student_c{size}"
    if registry.get_agent_factory(name) is None:
        registry.register_agent_factory(factory, name)
    return name


def build_student_run_config(cfg, agent: str, task_ids, seed: int, save_to: Path):
    """harvest.build_run_config's user, judge and limits; the agent is the student."""
    from tau2.data_model.simulation import TextRunConfig

    from src.harvest import DOMAIN

    return TextRunConfig(
        domain=DOMAIN,
        task_ids=task_ids,
        agent=agent,
        llm_agent=f"student:{cfg.model.base_model}",
        llm_args_agent={},
        user="user_simulator",
        llm_user=cfg.simulator.model,
        llm_args_user={
            "temperature": cfg.simulator.temperature,
            "extra_body": {"provider": {"allow_fallbacks": False}},
        },
        num_trials=1,
        seed=seed,
        max_steps=cfg.simulator.max_turns * 2,
        max_concurrency=1,
        max_retries=2,
        retry_delay=5.0,
        log_level="ERROR",
        save_to=str(save_to),
    )


def assert_same_harness_as_baseline(cfg) -> str:
    """Stage A students must face the simulator and judge the teacher faced."""
    from src.runlog import read_records

    log = cfg.paths.results_dir / "harvest-baseline.jsonl"
    recorded = {r["config_fingerprint"] for r in read_records(log) if not r.get("smoke")}
    live = cfg.teacher_fingerprint()
    if live not in recorded:
        raise InvariantViolation(
            f"teacher fingerprint {short(live)} (teacher, split, simulator, judge) is not the "
            f"one the baseline in {log} was recorded under ({sorted(short(f) for f in recorded)})"
        )
    return live


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("Free-running end-to-end evaluation (student drives)")
    parser.add_argument("--catalog", type=int, default=16)
    parser.add_argument(
        "--adapter", help="LoRA adapter: Hub repo id or local dir (omit: zero-shot)"
    )
    parser.add_argument("--backend", choices=["vllm", "hf", "fixed"], default="vllm")
    parser.add_argument("--max-model-len", type=int, default=32768)
    parser.add_argument("--batch", type=int, default=4, help="tasks per tau2 call")
    parser.add_argument("--label", help="names the results file (default: config + adapter tag)")
    parser.add_argument(
        "--hub-sync",
        metavar="REPO",
        help="private HF dataset repo that persists results across sessions",
    )
    args = parser.parse_args(argv)

    cfg = resolve(args)
    print(f"[eval_free] {', '.join(check_all(cfg))}")
    if args.catalog not in cfg.eval.catalog_sizes:
        raise InvariantViolation(f"catalog {args.catalog} not in {cfg.eval.catalog_sizes}")
    # The harness identity comes from the non-smoke config: --smoke shortens the
    # simulator (max_turns 2), which is fine for a plumbing check and never a result.
    from src.config import load_config

    harness = assert_same_harness_as_baseline(load_config(args.config))

    from src.harvest import (
        MAX_CONSECUTIVE_EMPTY_BATCHES,
        MAX_REQUESTS_PER_EPISODE,
        ProviderDown,
        _report_failures,
        affordable_episodes,
        batch_path,
        episode_reward,
        load_task_ids,
        split_task_ids,
    )
    from src.invariants import assert_split_disjoint
    from src.judge import install as install_judge
    from src.runlog import RunLog
    from src.throttle import DailyQuotaExhausted, RateLimiter, install

    limiter = RateLimiter(cfg.teacher.requests_per_minute, cfg.teacher.requests_per_day)
    install(limiter)
    install_judge(cfg.judge)

    train_ids, eval_ids = split_task_ids(
        load_task_ids(), cfg.split.n_train, cfg.split.n_eval, cfg.split.split_seed
    )
    assert_split_disjoint(train_ids, eval_ids)
    targets = eval_ids[: cfg.eval.n_tasks]
    variants = list(enumerate(cfg.eval.seeds))

    system_text = load_system_prompt(cfg)
    native = load_catalog(cfg, 16)
    catalog = load_catalog(cfg, args.catalog)
    catalog_names = {t["function"]["name"] for t in catalog}
    native_names = {t["function"]["name"] for t in native}

    if args.backend == "fixed":
        policy = FixedPolicy()
    elif args.backend == "vllm":
        policy = VLLMPolicy(cfg, args.adapter, args.max_model_len)
    else:
        policy = HFPolicy(cfg, args.adapter, args.max_model_len)
    agent = register_student_agent(args.catalog, catalog, native, system_text, policy)

    tag = args.label or (f"{cfg.name}-sft" if args.adapter else f"{cfg.name}-zeroshot")
    if args.backend == "fixed":
        tag = f"{cfg.name}-fixed"
    suffix = ".smoke" if cfg.smoke else ""
    identity = {
        "config": cfg.fingerprint(),
        "harness": harness,
        "catalog": args.catalog,
        "catalog_hash": cfg.eval.catalog_hashes[args.catalog],
        "prompt_template_hash": template_hash(),
        "adapter": args.adapter,
        "backend": args.backend,
    }
    log = RunLog(
        cfg.paths.results_dir / f"free-{tag}-c{args.catalog}{suffix}.jsonl",
        config_fingerprint=hash_obj(identity),
        config_name=f"free:{tag}:c{args.catalog}",
        smoke=cfg.smoke,
    )
    traj_dir = (
        cfg.paths.trajectories_dir / f"free-{tag}-c{args.catalog}{suffix}" / short(log.fingerprint)
    )
    if args.hub_sync:
        hub_pull(args.hub_sync, log.path, traj_dir)
    traj_dir.mkdir(parents=True, exist_ok=True)
    session = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    done = log.completed_cells()
    pending = [(t, rep, seed) for rep, seed in variants for t in targets if (t, rep) not in done]
    print(f"[eval_free] {tag} catalog {args.catalog} backend={args.backend} -> {log.path}")
    print(
        f"[eval_free] {len(done)}/{len(targets) * len(variants)} cells complete, {len(pending)} to run"
    )
    print(f"[eval_free] quota: {limiter.used_today} used today, {limiter.remaining_today} left")

    from tau2.registry import registry
    from tau2.runner.batch import run_tasks

    all_tasks = {str(t.id): t for t in registry.get_tasks_loader("retail")()}
    ran, empty_streak = 0, 0
    failed: list[tuple[int, float, str, str]] = []
    try:
        for rep, seed in variants:
            pool = [t for (t, r, _) in pending if r == rep]
            for i in range(0, len(pool), args.batch):
                chunk = pool[i : i + args.batch]
                affordable = affordable_episodes(limiter, len(chunk))
                if affordable == 0:
                    raise DailyQuotaExhausted(
                        f"{limiter.used_today}/{cfg.teacher.requests_per_day} used; stopping with "
                        f"{limiter.remaining_today} left (an episode may need {MAX_REQUESTS_PER_EPISODE})"
                    )
                chunk = chunk[:affordable]
                save_to = batch_path(traj_dir, session, rep, i // args.batch)
                if save_to.exists():
                    raise FileExistsError(f"{save_to} already exists; refusing to reuse it")
                run_cfg = build_student_run_config(cfg, agent, chunk, seed, save_to)
                results = run_tasks(
                    run_cfg, [all_tasks[t] for t in chunk], save_path=save_to, console_display=False
                )

                logged = 0
                for sim in getattr(results, "simulations", []):
                    tid = str(getattr(sim, "task_id", ""))
                    reward = episode_reward(sim)
                    if reward is None:
                        reason = getattr(getattr(sim, "termination_reason", None), "value", "?")
                        failed.append((rep, 0.0, tid, reason))
                        continue
                    msgs = [m.model_dump(mode="json") for m in (sim.messages or [])]
                    log.append(
                        task_id=tid,
                        seed=rep,
                        payload={
                            "catalog": args.catalog,
                            "tau2_seed": seed,
                            "reward": reward,
                            "termination": getattr(sim.termination_reason, "value", None),
                            "n_messages": len(msgs),
                            "trajectory_file": str(save_to),
                            **episode_stats(msgs, catalog_names, native_names),
                        },
                    )
                    logged += 1
                    ran += 1
                if args.hub_sync and save_to.exists():
                    hub_push(args.hub_sync, [log.path, save_to])
                dropped = len(chunk) - logged
                note = f", {dropped} failed (left unlogged to retry)" if dropped else ""
                print(
                    f"[eval_free] rep{rep} seed={seed} +{logged}{note} | "
                    f"{limiter.used_today} used, {limiter.remaining_today} left"
                )
                empty_streak = empty_streak + 1 if logged == 0 else 0
                if empty_streak >= MAX_CONSECUTIVE_EMPTY_BATCHES:
                    raise ProviderDown(f"{empty_streak} batches in a row logged nothing")
    except ProviderDown as exc:
        print(f"\n[eval_free] STOPPED EARLY: {exc}. Check the provider/network before rerunning.")
        _report_failures(failed)
        return 1
    except DailyQuotaExhausted as exc:
        print(f"\n[eval_free] daily budget spent: {exc}\n[eval_free] {ran} episodes this session.")
        _report_failures(failed)
        return 0

    print(f"\n[eval_free] done. {ran} episodes this session; trajectories in {traj_dir}")
    _report_failures(failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
