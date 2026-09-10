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
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

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
from src.invariants import check_all
from src.throttle import DailyQuotaExhausted, RateLimiter, install

DOMAIN = "retail"


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


def build_run_config(cfg, task_ids: list[str], temperature: float, seed: int, save_to: Path):
    """One tau2 batch: these tasks, this temperature, this seed."""
    from tau2.data_model.simulation import TextRunConfig

    return TextRunConfig(
        domain=DOMAIN,
        task_ids=task_ids,
        agent="llm_agent",
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
        "--dry-run",
        action="store_true",
        help="resolve config, split, quota and resume state without calling the API",
    )
    args = parser.parse_args(argv)

    cfg = resolve(args)
    print(f"[harvest] {', '.join(check_all(cfg))}")

    limiter = RateLimiter(cfg.teacher.requests_per_minute, cfg.teacher.requests_per_day)
    install(limiter)

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
    log = RunLog(
        args.out or cfg.paths.results_dir / f"harvest-{args.mode}{suffix}.jsonl",
        config_fingerprint=cfg.teacher_fingerprint(),
        config_name=f"{cfg.teacher.model}:{args.mode}",
        smoke=cfg.smoke,
    )
    done = log.completed_cells()
    pending = [
        (tid, rep, temp, seed)
        for rep, temp, seed in variants
        for tid in targets
        if (tid, rep) not in done
    ]

    traj_dir = cfg.paths.trajectories_dir / f"{cfg.name}-{args.mode}{suffix}"
    traj_dir.mkdir(parents=True, exist_ok=True)

    total = len(targets) * len(variants)
    print(f"[harvest] mode={args.mode} tasks={len(targets)} variants={len(variants)}")
    print(f"[harvest] {len(done)}/{total} cells complete, {len(pending)} to run")
    print(f"[harvest] quota: {limiter.used_today} used today, {limiter.remaining_today} left\n")

    if not pending:
        print("[harvest] nothing to do — this split is fully harvested.")
        return 0

    if args.dry_run:
        print("[harvest] DRY RUN — no API calls made.")
        print(f"[harvest] would run {len(pending)} episodes, ~{len(pending) * 13} requests")
        by_rep: dict[int, int] = {}
        for _, rep, _, _ in pending:
            by_rep[rep] = by_rep.get(rep, 0) + 1
        for rep, temp, seed in variants:
            print(f"[harvest]   rep{rep}: temp={temp} seed={seed} -> {by_rep.get(rep, 0)} episodes")
        print(f"[harvest] first 3 task ids: {[t for t, *_ in pending[:3]]}")
        print(f"[harvest] trajectories would land in {traj_dir}")
        return 0

    ran = 0
    try:
        for rep, temp, seed in variants:
            batch_pool = [t for (t, r, _, _) in pending if r == rep]
            for i in range(0, len(batch_pool), args.batch):
                chunk = batch_pool[i : i + args.batch]
                save_to = traj_dir / f"rep{rep}-batch{i // args.batch:03d}.json"
                run_cfg = build_run_config(cfg, chunk, temp, seed, save_to)

                from tau2.registry import registry
                from tau2.runner.batch import run_tasks

                tasks = [t for t in registry.get_tasks_loader(DOMAIN)() if str(t.id) in chunk]
                results = run_tasks(run_cfg, tasks, save_path=save_to, console_display=False)

                for sim in getattr(results, "simulations", []):
                    tid = str(getattr(sim, "task_id", ""))
                    reward = getattr(getattr(sim, "reward_info", None), "reward", None)
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
                    ran += 1
                print(
                    f"[harvest] rep{rep} temp={temp} +{len(chunk)} | "
                    f"{limiter.used_today} used, {limiter.remaining_today} left"
                )
    except DailyQuotaExhausted as exc:
        print(f"\n[harvest] daily budget spent: {exc}")
        print(f"[harvest] {ran} episodes this session. Rerun tomorrow to continue.")
        return 0

    print(f"\n[harvest] done. {ran} episodes this session, trajectories in {traj_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
