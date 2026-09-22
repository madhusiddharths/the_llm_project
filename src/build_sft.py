"""Harvested teacher episodes -> the SFT training set (plan §6, V1-04).

Local and free: reads cached trajectories, renders them through the shared
serializer, writes JSONL. Re-running after a serializer change takes seconds,
which is the point of keeping the harvest raw (see src/harvest.py).

One line per successful episode, as serializer segments ([text, train] pairs).
Not one line per decision: with causal attention, a whole episode with loss on
each teacher turn trains exactly the (prompt, decision) pairs teacher-forced
eval asks about, in one forward pass instead of ~10 (src/prompts.py,
serialize_episode). train.py tokenizes segments and masks the non-train ones.

What makes a file trustworthy, all recorded in its manifest and re-checked by
train.py before it spends GPU time:
  - teacher fingerprint (which harvest), rejection-sampled (reward 1.0 only)
  - prompt_template_hash, system_prompt_hash, catalog-16 hash (which prompt)
  - every task id is in the train split, none in eval

--smoke converts the first harvest.n_tasks train tasks (1 under smoke) of the
real harvest. The harvest identity always comes from the non-smoke config: a
smoke harvest is a different, much smaller set of episodes, and converting the
real one is already seconds of work.

    python src/build_sft.py --config configs/qwen05b.yaml [--smoke]
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.catalogs import load_catalog, load_system_prompt
from src.cli import build_parser, resolve
from src.config import ExperimentConfig, load_config
from src.hashing import hash_file, sha256_str, short
from src.invariants import InvariantViolation, check_all
from src.prompts import serialize_episode, template_hash
from src.trajectories import Episode, decision_steps, load_episodes

NATIVE_CATALOG = 16


def sft_dir(cfg: ExperimentConfig) -> Path:
    return Path("data/sft") / short(cfg.teacher_fingerprint())


def episode_record(episode: Episode, *, system: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
    segments = serialize_episode(system=system, tools=tools, messages=episode.messages)
    return {
        "task_id": episode.task_id,
        "rep": episode.rep,
        "temperature": episode.temperature,
        "n_decisions": len(decision_steps(episode.messages)),
        "chars": sum(len(s.text) for s in segments),
        "segments": [[s.text, s.train] for s in segments],
    }


def assert_train_only(episodes: list[Episode], train_ids: list[str], eval_ids: list[str]) -> None:
    """Training data may come from train-split tasks only (plan §6)."""
    ids = {e.task_id for e in episodes}
    if leaked := sorted(ids & set(eval_ids)):
        raise InvariantViolation(f"eval-split tasks in the training set: {leaked}")
    if stray := sorted(ids - set(train_ids)):
        raise InvariantViolation(f"tasks outside the train split in the training set: {stray}")


def build(
    episodes: list[Episode],
    *,
    system: str,
    tools: list[dict[str, Any]],
    out_path: Path,
) -> dict[str, Any]:
    records = [episode_record(e, system=system, tools=tools) for e in episodes]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    return {
        "n_episodes": len(records),
        "n_decisions": sum(r["n_decisions"] for r in records),
        "n_tasks": len({r["task_id"] for r in records}),
        "task_ids": sorted({r["task_id"] for r in records}, key=lambda t: (len(t), t)),
        "max_chars": max((r["chars"] for r in records), default=0),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser("Harvested teacher episodes -> SFT training set").parse_args(argv)
    cfg = resolve(args)  # smoke-aware: decides how much to convert
    harvest_cfg = load_config(args.config)  # never smoke: decides WHICH harvest
    print(f"[build_sft] {', '.join(check_all(cfg))}")

    from src.harvest import load_task_ids, split_task_ids
    from src.invariants import assert_split_disjoint

    train_ids, eval_ids = split_task_ids(
        load_task_ids(), cfg.split.n_train, cfg.split.n_eval, cfg.split.split_seed
    )
    assert_split_disjoint(train_ids, eval_ids)
    wanted = set(train_ids[: cfg.harvest.n_tasks])

    log_path = cfg.paths.results_dir / "harvest-harvest.jsonl"
    fingerprint = harvest_cfg.teacher_fingerprint()
    episodes = [e for e in load_episodes(log_path, fingerprint=fingerprint) if e.task_id in wanted]
    assert_train_only(episodes, train_ids, eval_ids)
    if not episodes:
        print(f"[build_sft] no successful episodes under {short(fingerprint)} in {log_path}")
        return 0

    system = load_system_prompt(cfg)
    tools = load_catalog(cfg, NATIVE_CATALOG)
    suffix = ".smoke" if cfg.smoke else ""
    out_dir = sft_dir(harvest_cfg)
    out_path = out_dir / f"train{suffix}.jsonl"
    stats = build(episodes, system=system, tools=tools, out_path=out_path)

    manifest = {
        "file": out_path.name,
        "file_sha256": hash_file(out_path),
        "created": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "smoke": cfg.smoke,
        "teacher_fingerprint": fingerprint,
        "prompt_template_hash": template_hash(),
        "system_prompt_hash": sha256_str(system),
        "catalog_size": NATIVE_CATALOG,
        "catalog_hash": cfg.eval.catalog_hashes[NATIVE_CATALOG],
        "rejection_sampled": True,
        **stats,
    }
    (out_dir / f"manifest{suffix}.json").write_text(json.dumps(manifest, indent=2) + "\n")

    print(
        f"[build_sft] {stats['n_episodes']} episodes, {stats['n_decisions']} decisions, "
        f"{stats['n_tasks']} tasks -> {out_path}"
    )
    print(f"[build_sft] longest episode: {stats['max_chars']:,} chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
