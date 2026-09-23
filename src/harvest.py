"""Teacher -> (state, action) pairs, paced to the provider's real limits.

What this does: runs the teacher through tau2-bench retail episodes and saves the
raw trajectories. It does NOT build training examples — that is a separate,
local, free conversion step. The separation matters: if the prompt format
changes, you re-convert in seconds instead of re-harvesting for six days.

Pacing: every litellm call goes through src.throttle, which holds the line at
20 requests/minute and 1,000/day, both account-wide. When the day's budget is
gone the run stops cleanly and the next run resumes from the log — nothing is
recomputed, because a repeated episode is spent quota.

    python src/harvest.py --config configs/qwen05b.yaml --smoke
    python src/harvest.py --config configs/qwen05b.yaml
    python src/harvest.py --config configs/qwen05b.yaml --mode baseline --catalog 80

--catalog N (baseline mode only; D7, 2026-09-22) shows the teacher the pinned
N-tool catalog: native tools plus BFCL distractors, in the catalog's own order.
The environment is unchanged, so a call to a distractor comes back as tau2's
"not found" error, just as a hallucinated tool would. Each catalog gets its own
log (harvest-baseline-c80.jsonl) and trajectory directory, keyed to the teacher
fingerprint plus the catalog hash, so the native baseline is never touched.
"""

from __future__ import annotations

import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# tau2 dumps its whole component registry at import time via loguru. Quiet it
# before the import, or every run buries its own output.
try:
    from loguru import logger as _loguru

    _loguru.remove()
    _loguru.add(sys.stderr, level="WARNING")
except ImportError:  # loguru only present once tau2 is installed
    pass

from src.cli import build_parser, resolve
from src.hashing import short
from src.invariants import check_all
from src.judge import install as install_judge
from src.throttle import DailyQuotaExhausted, RateLimiter, install

DOMAIN = "retail"

# MEASURED 2026-09-11 over 59 real episodes: 15.7 requests/episode mean, 22 max.
#
# The guard below budgets with the MAX, not the mean, because an episode killed
# at 90% has still spent everything it spent. The first full run ended 73 requests
# — 7% of a day — inside episodes that were abandoned mid-flight, and that is the
# whole cost this constant exists to avoid.
#
# Deliberately NOT a config field: it is a tuning number that wants re-measuring
# as episodes get longer, and everything in TeacherConfig is inside
# teacher_fingerprint(), so putting it there would orphan an in-progress harvest
# every time the estimate was refined.
MAX_REQUESTS_PER_EPISODE = 22

# Stop when this many batches in a row log nothing. 2026-09-16: the teacher's
# only provider returned 404 for a whole day and the run burned 981 requests on
# 166 failed episodes. 2026-09-20: the laptop woke without network and 40
# episodes failed in six minutes. Either way every remaining episode would fail
# too, and each one still costs quota. Two batches (~8 episodes) is enough
# evidence; an unlucky single task never empties a whole batch.
MAX_CONSECUTIVE_EMPTY_BATCHES = 2
NATIVE_CATALOG = 16


class ProviderDown(RuntimeError):
    """Consecutive batches produced nothing: stop before spending more quota."""


def load_task_ids() -> list[str]:
    """Every retail task id, in the order tau2 ships them."""
    from tau2.registry import registry

    tasks = registry.get_tasks_loader(DOMAIN)()
    return [str(t.id) for t in tasks]


def split_task_ids(
    task_ids: list[str], n_train: int, n_eval: int, seed: int
) -> tuple[list[str], list[str]]:
    """Split by task ID, never by step (plan §6).

    Deterministic in the split seed, so the same split comes back on every
    machine and every rerun. invariants.assert_split_disjoint checks the result.
    """
    if n_train + n_eval > len(task_ids):
        raise ValueError(f"split wants {n_train + n_eval} tasks, domain has {len(task_ids)}")
    shuffled = list(task_ids)
    random.Random(seed).shuffle(shuffled)
    return shuffled[:n_train], shuffled[n_train : n_train + n_eval]


def order_agent_tools(env_tools: dict[str, Any], catalog: list[dict[str, Any]], make_stub) -> list:
    """The agent's tool list in catalog order: the environment's real Tool object
    for each native name, and make_stub(schema) for each distractor. Pure, so the
    ordering is testable without tau2."""
    missing = set(env_tools) - {t["function"]["name"] for t in catalog}
    if missing:
        raise ValueError(f"catalog is missing native tools {sorted(missing)}")
    return [env_tools.get(t["function"]["name"]) or make_stub(t) for t in catalog]


def register_padded_agent(catalog: list[dict[str, Any]], size: int) -> str:
    """Register tau2 agent factory 'llm_agent_c<size>' and return its name.

    Only the AGENT's tool list is padded. tau2 builds the agent from
    environment.get_tools() (runner/build.py) and executes calls against the
    environment, which never learns about the distractors.
    """
    from tau2.agent.llm_agent import LLMAgent
    from tau2.environment.tool import BaseTool
    from tau2.registry import registry

    class DistractorTool(BaseTool):
        tool_schema: dict

        @property
        def openai_schema(self) -> dict:
            return self.tool_schema

        def _call(self, *args, **kwargs):  # never reached: the env executes calls
            raise RuntimeError(f"distractor {self.name} was executed")

    def factory(tools, domain_policy, **kwargs):
        ordered = order_agent_tools(
            {t.name: t for t in tools},
            catalog,
            lambda t: DistractorTool(name=t["function"]["name"], tool_schema=t),
        )
        return LLMAgent(
            tools=ordered,
            domain_policy=domain_policy,
            llm=kwargs.get("llm"),
            llm_args=kwargs.get("llm_args"),
        )

    name = f"llm_agent_c{size}"
    if registry.get_agent_factory(name) is None:
        registry.register_agent_factory(factory, name)
    return name


def build_run_config(
    cfg,
    task_ids: list[str],
    temperature: float,
    seed: int,
    save_to: Path,
    agent: str = "llm_agent",
):
    """One tau2 batch: these tasks, this temperature, this seed."""
    from tau2.data_model.simulation import TextRunConfig

    return TextRunConfig(
        domain=DOMAIN,
        task_ids=task_ids,
        agent=agent,
        llm_agent=f"openrouter/{cfg.teacher.model}",
        llm_args_agent={
            "temperature": temperature,
            "max_tokens": cfg.teacher.max_tokens,
            # Pin the upstream provider: OpenRouter routes :free variants across
            # providers, and a quantisation change mid-harvest would give us a
            # teacher that is not the teacher we started with.
            "extra_body": {"provider": {"allow_fallbacks": False}},
        },
        user="user_simulator",
        llm_user=cfg.simulator.model,
        llm_args_user={
            "temperature": cfg.simulator.temperature,
            "extra_body": {"provider": {"allow_fallbacks": False}},
        },
        num_trials=1,
        seed=seed,
        max_steps=cfg.simulator.max_turns * 2,
        max_concurrency=1,  # the throttle governs pacing, not tau2
        max_retries=2,  # low on purpose: retries into a rate limit made it worse
        retry_delay=5.0,
        log_level="ERROR",
        save_to=str(save_to),
    )


def batch_path(traj_dir: Path, session: str, rep: int, index: int) -> Path:
    """Where one tau2 batch is saved. Unique per session, never reused.

    The index counts batches of the PENDING pool, which restarts at 0 every
    session. Named by index alone, day 2's rep1-batch000 (tasks 61, 94, 101, 0)
    collided with day 1's file of that name (tasks 22, 63, 79, 108), and tau2
    stopped on an interactive y/n prompt where both answers raise: n is a
    FileExistsError, y is "Tasks were removed from the task set". Found
    2026-09-13. Naming by task ids would not fix it either — a batch retried after
    failing holds the same tasks as the file it failed in. The session stamp does.

    Earlier files are never touched, so the trajectory_file in every logged
    record keeps pointing at the episodes it describes.
    """
    return traj_dir / f"rep{rep}-{session}-batch{index:03d}.json"


def episode_reward(sim) -> float | None:
    """The reward tau2 scored, or None when the episode never produced one.

    None is not "scored zero". tau2 returns a SimulationRun for a task that
    failed every attempt — INFRASTRUCTURE_ERROR, no messages, reward_info unset —
    and the caller must tell that apart from a genuine 0.0, because one is a cell
    to record and the other is a cell to retry.
    """
    return getattr(getattr(sim, "reward_info", None), "reward", None)


def affordable_episodes(limiter: RateLimiter, wanted: int) -> int:
    """How many of `wanted` episodes today's remaining budget can fund in full.

    Why this exists rather than just letting limiter.acquire() raise: tau2 runs
    each task inside `except Exception`, so DailyQuotaExhausted never reaches the
    handler in main(). Instead tau2 reads it as a task failure, burns its two
    retries, marks the task INFRASTRUCTURE_ERROR and moves to the next batch —
    which on 2026-09-11 marched through all 25 remaining batches printing errors
    for a budget that was already gone. The limiter cannot fix that from its side;
    the runner has to stop asking.
    """
    remaining = limiter.remaining_today
    if remaining is None:  # no daily cap configured
        return wanted
    return min(wanted, remaining // MAX_REQUESTS_PER_EPISODE)


def _report_failures(failed: list[tuple[int, float, str, str]]) -> None:
    """Say out loud what did not get recorded.

    These cells are deliberately absent from the run log, so the next run picks
    them up. That is only safe if the session that dropped them says so — a
    silent drop is the exact failure this whole module is arranged to avoid.
    """
    if not failed:
        return
    print(f"[harvest] {len(failed)} episode(s) failed and were NOT logged; the next run retries.")

    # Grouped, not one line per episode. The first version printed 103 identical
    # "infrastructure_error" lines, which said nothing about what had happened.
    reasons: dict[str, int] = {}
    for *_, reason in failed:
        reasons[reason] = reasons.get(reason, 0) + 1
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"[harvest]   {reason}: {count}")

    by_rep: dict[tuple[int, float], list[str]] = {}
    for rep, temp, tid, _ in failed:
        by_rep.setdefault((rep, temp), []).append(tid)
    for (rep, temp), tids in sorted(by_rep.items()):
        head = ", ".join(tids[:8])
        more = f", +{len(tids) - 8} more" if len(tids) > 8 else ""
        print(f"[harvest]   rep{rep} temp={temp}: {len(tids)} tasks ({head}{more})")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("Harvest teacher trajectories from tau2-bench retail")
    parser.add_argument(
        "--mode",
        choices=["harvest", "baseline"],
        default="harvest",
        help="harvest = training split, 3 temperatures. baseline = eval split, 5 seeds.",
    )
    parser.add_argument("--batch", type=int, default=4, help="tasks per tau2 call")
    parser.add_argument(
        "--catalog",
        type=int,
        default=NATIVE_CATALOG,
        help="tool catalog the teacher sees (baseline mode; 16 = native)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve config, split, quota and resume state without calling the API",
    )
    args = parser.parse_args(argv)

    cfg = resolve(args)
    print(f"[harvest] {', '.join(check_all(cfg))}")
    padded = args.catalog != NATIVE_CATALOG
    if padded and args.mode != "baseline":
        parser.error("--catalog is for --mode baseline; training data uses the native tools")

    limiter = RateLimiter(cfg.teacher.requests_per_minute, cfg.teacher.requests_per_day)
    install(limiter)
    # Before any episode runs: tau2's NL-assertion judge defaults to gpt-4.1 and
    # we hold no OpenAI key, which silently killed 33% of the first harvest.
    install_judge(cfg.judge)
    print(f"[harvest] judge: {cfg.judge.model} @ temp={cfg.judge.temperature}")

    all_ids = load_task_ids()
    train_ids, eval_ids = split_task_ids(
        all_ids, cfg.split.n_train, cfg.split.n_eval, cfg.split.split_seed
    )

    from src.invariants import assert_split_disjoint

    assert_split_disjoint(train_ids, eval_ids)

    if args.mode == "harvest":
        targets = train_ids[: cfg.harvest.n_tasks]
        variants = [
            (i, t, cfg.seed)
            for i, t in enumerate(cfg.teacher.temperatures[: cfg.harvest.samples_per_task])
        ]
    else:
        targets = eval_ids[: cfg.eval.n_tasks]
        variants = [(i, 0.0, s) for i, s in enumerate(cfg.eval.seeds)]

    # Keyed to the teacher, not the student: see ExperimentConfig.teacher_fingerprint.
    from src.runlog import RunLog

    suffix = ".smoke" if cfg.smoke else ""
    agent_name, catalog_tag, fingerprint = "llm_agent", "", cfg.teacher_fingerprint()
    if padded:
        from src.catalogs import load_catalog
        from src.hashing import hash_obj

        agent_name = register_padded_agent(load_catalog(cfg, args.catalog), args.catalog)
        catalog_tag = f"-c{args.catalog}"
        fingerprint = hash_obj(
            {
                "teacher": cfg.teacher_fingerprint(),
                "catalog": args.catalog,
                "catalog_hash": cfg.eval.catalog_hashes[args.catalog],
            }
        )
        print(f"[harvest] teacher sees catalog {args.catalog} via agent {agent_name}")
    log = RunLog(
        args.out or cfg.paths.results_dir / f"harvest-{args.mode}{catalog_tag}{suffix}.jsonl",
        config_fingerprint=fingerprint,
        config_name=f"{cfg.teacher.model}:{args.mode}{catalog_tag}",
        smoke=cfg.smoke,
    )
    done = log.completed_cells()
    pending = [
        (tid, rep, temp, seed)
        for rep, temp, seed in variants
        for tid in targets
        if (tid, rep) not in done
    ]

    # Nested under the teacher fingerprint, because batch filenames restart at 000
    # for every run. Without this, changing the judge (or anything else the harvest
    # is keyed on) would overwrite rep0-batch000.json while the old log records
    # still pointed at that name — auditable records aimed at the wrong episodes.
    traj_dir = (
        cfg.paths.trajectories_dir
        / f"{cfg.name}-{args.mode}{catalog_tag}{suffix}"
        / short(fingerprint)
    )
    traj_dir.mkdir(parents=True, exist_ok=True)
    session = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    total = len(targets) * len(variants)
    print(f"[harvest] mode={args.mode} tasks={len(targets)} variants={len(variants)}")
    print(f"[harvest] {len(done)}/{total} cells complete, {len(pending)} to run")
    print(f"[harvest] quota: {limiter.used_today} used today, {limiter.remaining_today} left\n")

    if not pending:
        print("[harvest] nothing to do — this split is fully harvested.")
        return 0

    if args.dry_run:
        print("[harvest] DRY RUN — no API calls made.")
        print(
            f"[harvest] would run {len(pending)} episodes, "
            f"~{round(len(pending) * 15.7)} requests (measured mean; up to {len(pending) * MAX_REQUESTS_PER_EPISODE} worst case)"
        )
        by_rep: dict[int, int] = {}
        for _, rep, _, _ in pending:
            by_rep[rep] = by_rep.get(rep, 0) + 1
        for rep, temp, seed in variants:
            print(f"[harvest]   rep{rep}: temp={temp} seed={seed} -> {by_rep.get(rep, 0)} episodes")
        print(f"[harvest] first 3 task ids: {[t for t, *_ in pending[:3]]}")
        print(f"[harvest] trajectories would land in {traj_dir}")
        return 0

    ran = 0
    empty_streak = 0
    failed: list[tuple[int, float, str, str]] = []
    try:
        for rep, temp, seed in variants:
            batch_pool = [t for (t, r, _, _) in pending if r == rep]
            for i in range(0, len(batch_pool), args.batch):
                chunk = batch_pool[i : i + args.batch]
                # Never start an episode today's budget cannot finish. See
                # affordable_episodes: the limiter's own exception cannot stop the
                # run, because tau2 swallows it.
                affordable = affordable_episodes(limiter, len(chunk))
                if affordable == 0:
                    raise DailyQuotaExhausted(
                        f"{limiter.used_today}/{cfg.teacher.requests_per_day} requests used; "
                        f"stopping with {limiter.remaining_today} left rather than starting an "
                        f"episode that needs up to {MAX_REQUESTS_PER_EPISODE}."
                    )
                chunk = chunk[:affordable]
                save_to = batch_path(traj_dir, session, rep, i // args.batch)
                if save_to.exists():
                    # Fail loudly instead of reaching tau2's resume prompt, which
                    # would hang an unattended run on input() for the whole night.
                    raise FileExistsError(f"{save_to} already exists; refusing to reuse it")
                run_cfg = build_run_config(cfg, chunk, temp, seed, save_to, agent=agent_name)

                from tau2.registry import registry
                from tau2.runner.batch import run_tasks

                tasks = [t for t in registry.get_tasks_loader(DOMAIN)() if str(t.id) in chunk]
                results = run_tasks(run_cfg, tasks, save_path=save_to, console_display=False)

                logged = 0
                for sim in getattr(results, "simulations", []):
                    tid = str(getattr(sim, "task_id", ""))
                    reward = episode_reward(sim)
                    if reward is None:
                        # When a task fails every attempt, tau2 still returns a
                        # SimulationRun — INFRASTRUCTURE_ERROR, no messages, no
                        # reward_info. Logging it would mark the cell complete, so
                        # completed_cells() would skip it forever and the harvest
                        # would finish short of n_tasks without printing an error.
                        # An episode that produced nothing stays unlogged, so the
                        # next run retries it. Cost of getting this wrong, measured
                        # 2026-09-10: 18 of 54 train tasks written off in silence.
                        reason = getattr(getattr(sim, "termination_reason", None), "value", "?")
                        failed.append((rep, temp, tid, reason))
                        continue
                    log.append(
                        task_id=tid,
                        seed=rep,
                        payload={
                            "mode": args.mode,
                            "temperature": temp,
                            "tau2_seed": seed,
                            "reward": reward,
                            "trajectory_file": str(save_to),
                            "n_messages": len(getattr(sim, "messages", []) or []),
                        },
                    )
                    logged += 1
                    ran += 1
                dropped = len(chunk) - logged
                note = f", {dropped} failed (left unlogged to retry)" if dropped else ""
                print(
                    f"[harvest] rep{rep} temp={temp} +{logged}{note} | "
                    f"{limiter.used_today} used, {limiter.remaining_today} left"
                )
                empty_streak = empty_streak + 1 if logged == 0 else 0
                if empty_streak >= MAX_CONSECUTIVE_EMPTY_BATCHES:
                    raise ProviderDown(
                        f"{empty_streak} batches in a row logged nothing; the provider or the "
                        "network is down. Check it before rerunning (see _report_failures)."
                    )
    except ProviderDown as exc:
        print(f"\n[harvest] STOPPED EARLY: {exc}")
        print(f"[harvest] {ran} episodes this session; quota left: {limiter.remaining_today}.")
        _report_failures(failed)
        return 1
    except DailyQuotaExhausted as exc:
        print(f"\n[harvest] daily budget spent: {exc}")
        print(f"[harvest] {ran} episodes this session. Rerun tomorrow to continue.")
        _report_failures(failed)
        return 0

    print(f"\n[harvest] done. {ran} episodes this session, trajectories in {traj_dir}")
    _report_failures(failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
