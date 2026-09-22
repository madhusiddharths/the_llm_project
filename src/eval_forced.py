"""Teacher-forced replay: the per-step number (plan §4, V1-05).

Replay a logged teacher trajectory from the EVAL split. At each teacher
decision, show the student the exact prompt the teacher's conversation produced
and compare its answer with what the teacher did. Then advance using the
TEACHER's action regardless of what the student said. That detail is the whole
method: here it holds by construction, because every prompt is rendered from
the teacher's recorded messages and nothing the student says is ever fed back.

The GPU half is kept dumb on purpose. This script renders prompts and scores
completions, both pure Python on the Mac. Whatever produces the completions
(vLLM or HF on Kaggle) only maps prompt -> text, so the serializer and the
scoring rules can never drift between machines.

    # 1. sanity check: the teacher against itself must score 100%
    python src/eval_forced.py --catalog 16 --backend teacher
    # 2. export prompts for a GPU run
    python src/eval_forced.py --catalog 16 --export data/forced/prompts-c16.jsonl
    # 3. score completions that came back from the GPU run
    python src/eval_forced.py --catalog 16 --backend file --completions results/completions-....jsonl

Which teacher episodes are replayed (D6, open, a metric definition that is
Madhu's call): --episodes successful (default) replays only reward-1.0 baseline
episodes, so the reference action is one that led to a solved task. --episodes
all replays every scored episode, which covers all 60 eval tasks but compares
students against teacher actions that sometimes led to failure.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.catalogs import load_catalog, load_system_prompt
from src.cli import build_parser, resolve
from src.config import load_config
from src.hashing import hash_file, hash_obj, short
from src.invariants import InvariantViolation, check_all
from src.metrics import score_step, summarize
from src.prompts import IM_END, parse_completion, render_action, serialize_state, template_hash
from src.runlog import RunLog, read_records
from src.trajectories import Action, Episode, decision_steps, load_episodes


@dataclass(frozen=True)
class EvalStep:
    step_id: str  # "<task>:<rep>:<message index>", stable across runs
    task_id: str
    rep: int
    index: int
    prompt: str
    reference: Action


def eval_steps(
    episodes: Iterable[Episode], *, system: str, tools: list[dict[str, Any]]
) -> list[EvalStep]:
    steps = []
    for ep in episodes:
        for st in decision_steps(ep.messages):
            steps.append(
                EvalStep(
                    step_id=f"{ep.task_id}:{ep.rep}:{st.index}",
                    task_id=ep.task_id,
                    rep=ep.rep,
                    index=st.index,
                    prompt=serialize_state(system=system, tools=tools, messages=st.context),
                    reference=st.action,
                )
            )
    return steps


def teacher_completions(steps: Iterable[EvalStep]) -> dict[str, str]:
    """The teacher's own actions, rendered as if generated. Must score 100%."""
    return {s.step_id: render_action(s.reference) + IM_END for s in steps}


def file_completions(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for rec in read_records(path):
        if rec["step_id"] in out:
            raise ValueError(f"{path}: duplicate completion for step {rec['step_id']}")
        out[rec["step_id"]] = rec["completion"]
    return out


def score_steps(
    steps: list[EvalStep], completions: dict[str, str], catalog_names: list[str]
) -> dict[tuple[str, int], list[dict[str, Any]]]:
    """Per-episode lists of step scores. A missing completion is an error: a
    silently skipped step would inflate agreement on exactly the hard cases."""
    missing = [s.step_id for s in steps if s.step_id not in completions]
    if missing:
        raise ValueError(f"{len(missing)} steps have no completion, e.g. {missing[:3]}")
    by_episode: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for s in steps:
        parsed = parse_completion(completions[s.step_id])
        row = {
            "step_id": s.step_id,
            "index": s.index,
            **score_step(s.reference, parsed, catalog_names).as_dict(),
        }
        by_episode.setdefault((s.task_id, s.rep), []).append(row)
    return by_episode


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("Teacher-forced per-step evaluation")
    parser.add_argument("--catalog", type=int, default=16, help="tool catalog size (16/40/80)")
    parser.add_argument("--backend", choices=["teacher", "file"], default="teacher")
    parser.add_argument(
        "--completions", type=Path, help="JSONL of {step_id, completion} (--backend file)"
    )
    parser.add_argument("--export", type=Path, help="write prompts JSONL for a GPU run and exit")
    parser.add_argument("--episodes", choices=["successful", "all"], default="successful")
    args = parser.parse_args(argv)

    cfg = resolve(args)  # smoke-aware: how many tasks and reps
    baseline_cfg = load_config(args.config)  # never smoke: which baseline run
    print(f"[eval_forced] {', '.join(check_all(cfg))}")
    if args.catalog not in cfg.eval.catalog_sizes:
        raise InvariantViolation(
            f"catalog {args.catalog} is not in eval.catalog_sizes {cfg.eval.catalog_sizes}"
        )

    from src.harvest import load_task_ids, split_task_ids
    from src.invariants import assert_split_disjoint

    train_ids, eval_ids = split_task_ids(
        load_task_ids(), cfg.split.n_train, cfg.split.n_eval, cfg.split.split_seed
    )
    assert_split_disjoint(train_ids, eval_ids)
    wanted_reps = set(range(len(cfg.eval.seeds)))

    fingerprint = baseline_cfg.teacher_fingerprint()
    episodes = load_episodes(
        cfg.paths.results_dir / "harvest-baseline.jsonl",
        fingerprint=fingerprint,
        successful_only=args.episodes == "successful",
    )
    if stray := sorted({e.task_id for e in episodes} - set(eval_ids)):
        raise InvariantViolation(f"baseline episodes outside the eval split: {stray}")
    episodes = [e for e in episodes if e.rep in wanted_reps]
    # The first eval.n_tasks eval tasks that have an episode to replay (all 60
    # in a real run). Counting only tasks with episodes keeps --smoke from
    # landing on two tasks the teacher never solved and scoring nothing.
    present = {e.task_id for e in episodes}
    wanted_tasks = set([t for t in eval_ids if t in present][: cfg.eval.n_tasks])
    episodes = [e for e in episodes if e.task_id in wanted_tasks]

    system = load_system_prompt(cfg)
    tools = load_catalog(cfg, args.catalog)
    steps = eval_steps(episodes, system=system, tools=tools)
    print(
        f"[eval_forced] catalog {args.catalog}: {len(episodes)} {args.episodes} episodes, "
        f"{len(steps)} steps, {len({e.task_id for e in episodes})} tasks"
    )

    if args.export:
        args.export.parent.mkdir(parents=True, exist_ok=True)
        with args.export.open("w") as fh:
            for s in steps:
                fh.write(
                    json.dumps({"step_id": s.step_id, "prompt": s.prompt}, ensure_ascii=False)
                    + "\n"
                )
        print(f"[eval_forced] wrote {len(steps)} prompts to {args.export}")
        return 0

    if args.backend == "teacher":
        completions, source = teacher_completions(steps), "teacher"
    else:
        if args.completions is None:
            parser.error("--backend file needs --completions")
        completions, source = file_completions(args.completions), hash_file(args.completions)

    identity = {
        "config": cfg.fingerprint(),
        "baseline": fingerprint,
        "catalog": args.catalog,
        "catalog_hash": cfg.eval.catalog_hashes[args.catalog],
        "prompt_template_hash": template_hash(),
        "episodes": args.episodes,
        "completions": source,
    }
    label = "teacher" if args.backend == "teacher" else cfg.name
    suffix = ".smoke" if cfg.smoke else ""
    log = RunLog(
        cfg.paths.results_dir / f"forced-{label}-c{args.catalog}{suffix}.jsonl",
        config_fingerprint=hash_obj(identity),
        config_name=f"forced:{label}:c{args.catalog}",
        smoke=cfg.smoke,
    )
    done = log.completed_cells()
    scored = score_steps(steps, completions, [t["function"]["name"] for t in tools])
    for (task_id, rep), rows in scored.items():
        if (task_id, rep) in done:
            continue
        log.append(
            task_id=task_id,
            seed=rep,
            payload={
                "catalog": args.catalog,
                "backend": args.backend,
                "identity": identity,
                "steps": rows,
            },
        )

    rows = [
        row
        for rec in read_records(log.path)
        if rec["config_fingerprint"] == log.fingerprint
        for row in rec["steps"]
    ]
    summary = summarize(rows)
    print(f"[eval_forced] {short(log.fingerprint)} -> {log.path}")
    for key, value in summary.items():
        print(f"[eval_forced]   {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
