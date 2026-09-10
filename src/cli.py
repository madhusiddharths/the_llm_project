"""Shared CLI surface. Every script in src/ uses this; none rolls its own.

division-of-labor.md: "Every script gets a --smoke flag. Same code path, tiny
inputs, finishes in under two minutes." The override set lives in
config.SMOKE_OVERRIDES and is applied at load time, so --smoke changes the
inputs and never the logic — there is no `if smoke:` branch anywhere in src/.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from src.config import ExperimentConfig, load_config
from src.invariants import check_all
from src.runlog import RunLog

DEFAULT_CONFIG = "configs/qwen05b.yaml"

SCAFFOLD_BANNER = "SCAFFOLD ONLY — no model work performed"


def build_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"experiment config YAML (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="tiny inputs, same code path, finishes in under two minutes",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="override the config seed (must be explicit; never defaulted)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="results JSONL (default: <results_dir>/<config name>.jsonl)",
    )
    return parser


def resolve(args: argparse.Namespace) -> ExperimentConfig:
    """The only path from CLI args to a validated config."""
    return load_config(args.config, smoke=args.smoke, seed=args.seed)


def results_path(cfg: ExperimentConfig, args: argparse.Namespace) -> Path:
    if args.out is not None:
        return args.out
    suffix = ".smoke" if cfg.smoke else ""
    return cfg.paths.results_dir / f"{cfg.name}{suffix}.jsonl"


def open_runlog(cfg: ExperimentConfig, args: argparse.Namespace) -> RunLog:
    return RunLog(
        results_path(cfg, args),
        config_fingerprint=cfg.fingerprint(),
        config_name=cfg.name,
        smoke=cfg.smoke,
    )


def run_scaffold(script: str, cfg: ExperimentConfig, log: RunLog) -> int:
    """Exercise the plumbing every real script will use, and nothing else.

    Loads config, runs the invariants, writes one record, and proves the
    resume-skip path works. Week 1 replaces the body of each script; the harness
    it runs on is already proven by then.
    """
    checks = check_all(cfg)
    print(f"[{script}] {SCAFFOLD_BANNER}")
    print(f"[{script}] config={cfg.name} fingerprint={cfg.short_fingerprint()} smoke={cfg.smoke}")
    print(f"[{script}] invariants passed: {', '.join(checks)}")

    task_id, seed = f"__scaffold__{script}", cfg.eval.seeds[0]
    before = log.completed_cells()
    if (task_id, seed) in before:
        print(f"[{script}] cell already complete, skipping — resume path works")
    else:
        log.append(task_id=task_id, seed=seed, payload={"scaffold": True, "script": script})
        print(f"[{script}] wrote 1 scaffold record to {log.path}")

    assert not log.should_run(task_id, seed), "resume-skip failed after append"
    print(f"[{script}] resume-skip verified; {len(log)} cell(s) complete")
    return 0


def stub_main(script: str, description: str, plan_ref: str, argv: list[str] | None = None) -> int:
    """Entry point for a script whose body lands in a later week.

    Under --smoke it exercises the shared plumbing and exits 0. Without --smoke
    it refuses to run, so a stub can never be mistaken for a working pipeline.
    """
    args = build_parser(description).parse_args(argv)
    cfg = resolve(args)

    if not cfg.smoke:
        raise NotImplementedError(
            f"{script} is scaffolding only — its body lands in {plan_ref}.\n"
            f"Run `python src/{script}.py --smoke` to exercise the shared plumbing."
        )

    return run_scaffold(script, cfg, open_runlog(cfg, args))
