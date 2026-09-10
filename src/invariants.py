"""Assertions that abort a run rather than let it produce plausible garbage.

Every check here exists because the plan names a specific way this project can
fail quietly: §7's three constraints, the Red Flags list, and the nine-item
pre-launch checklist. There is deliberately no warn-and-continue mode — a run
that violates one of these produces numbers that look fine and mean nothing,
which is strictly worse than no run at all.

Acceptance test (division-of-labor.md): deliberately mismatch a prompt hash and
the run aborts. See tests/test_invariants.py.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from pathlib import Path

from src.config import ExperimentConfig, SimulatorConfig
from src.hashing import hash_file, short
from src.prompts import template_hash

MIN_SEEDS = 5  # §10: "5 seeds minimum, report CIs"


class InvariantViolation(RuntimeError):
    """Raised when a run must not proceed. Never caught inside src/."""


def assert_prompt_template_match(*configs: ExperimentConfig) -> None:
    """The pinned hash in every config must equal the live serializer's hash.

    Plan §10 ranks prompt-format drift between train and eval a top-three risk,
    and §6 Gate 2 says it is the cause roughly 80% of the time. This is the one
    check most worth having on day one.
    """
    if not configs:
        raise ValueError("assert_prompt_template_match needs at least one config")

    live = template_hash()
    for cfg in configs:
        if cfg.prompt_template_hash != live:
            raise InvariantViolation(
                f"prompt template hash mismatch for config '{cfg.name}':\n"
                f"  pinned in config : {short(cfg.prompt_template_hash)}\n"
                f"  live serializer  : {short(live)}\n"
                "The shared serializer changed. If that change was reviewed, update "
                "prompt_template_hash in configs/base.yaml (`make prompt-hash`). If it "
                "was not, revert src/prompts.py — train and eval have drifted apart."
            )

    pinned = {cfg.prompt_template_hash for cfg in configs}
    if len(pinned) > 1:
        names = ", ".join(cfg.name for cfg in configs)
        raise InvariantViolation(f"configs disagree on prompt_template_hash: {names}")


def assert_catalog_hash(cfg: ExperimentConfig, size: int, path: str | Path) -> None:
    """A tool catalog on disk must match the hash pinned in the config."""
    path = Path(path)
    if not path.exists():
        raise InvariantViolation(f"catalog for size {size} not found at {path}")

    pinned = cfg.eval.catalog_hashes.get(size)
    if pinned is None:
        raise InvariantViolation(
            f"catalog size {size} exists at {path} but is not pinned in "
            f"eval.catalog_hashes. Pin it before any run that reports numbers."
        )

    actual = hash_file(path)
    if actual != pinned:
        raise InvariantViolation(
            f"catalog {size} hash mismatch: pinned {short(pinned)}, on disk {short(actual)}"
        )


FLOATING_ALIASES = ("latest", "preview")


def assert_teacher_model_pinned(cfg: ExperimentConfig) -> None:
    """The teacher must be a fixed version, not a floating alias.

    Providers rotate what "-latest" points at without notice. If the teacher
    changes between the week 1 harvest and the week 3 baseline, the students
    were distilled from one model and compared against another — and nothing in
    the output would look wrong. Same failure class as prompt drift, so it gets
    the same treatment: abort.
    """
    model = cfg.teacher.model.lower()
    hits = [alias for alias in FLOATING_ALIASES if alias in model]
    if hits:
        raise InvariantViolation(
            f"teacher model '{cfg.teacher.model}' contains {hits}, which is a floating "
            "alias the provider can repoint at any time. Pin an explicit version — the "
            "teacher must be frozen for the whole project or the comparison is void."
        )


def assert_seed_discipline(cfg: ExperimentConfig) -> None:
    """Seeds set explicitly, and enough of them to compute a CI."""
    if cfg.simulator.seed is None:
        raise InvariantViolation("simulator.seed must be set explicitly")
    if not cfg.eval.seeds:
        raise InvariantViolation("eval.seeds is empty")
    if not cfg.smoke and len(cfg.eval.seeds) < MIN_SEEDS:
        raise InvariantViolation(
            f"eval.seeds has {len(cfg.eval.seeds)} entries, need >= {MIN_SEEDS} "
            "to report confidence intervals (plan §10). Smoke runs are exempt."
        )


def assert_split_disjoint(train_ids: Iterable[str], eval_ids: Iterable[str]) -> None:
    """Split by task ID, never by step (§6). Any overlap leaks eval into train."""
    train_set, eval_set = set(train_ids), set(eval_ids)
    overlap = train_set & eval_set
    if overlap:
        sample = ", ".join(sorted(overlap)[:5])
        raise InvariantViolation(
            f"{len(overlap)} task IDs appear in both train and eval (e.g. {sample}). "
            "The split must be by task, never by step."
        )
    if not train_set or not eval_set:
        raise InvariantViolation("train and eval splits must both be non-empty")


def assert_simulator_frozen(cfg: ExperimentConfig, reference: SimulatorConfig) -> None:
    """User-simulator settings identical across every config (§7 constraint 2).

    The in-house simulator is only defensible because it is held constant in the
    comparison. If it moves between configs, the ladder measures the simulator.
    """
    if cfg.simulator != reference:
        diffs = [
            f"    {field}: {getattr(cfg.simulator, field)!r} != {getattr(reference, field)!r}"
            for field in type(reference).model_fields
            if getattr(cfg.simulator, field) != getattr(reference, field)
        ]
        raise InvariantViolation(
            f"simulator config drifted in '{cfg.name}':\n"
            + "\n".join(diffs)
            + "\n  The simulator must be frozen across all configs, or the "
            "comparison across model sizes is meaningless."
        )


def assert_results_appendable(path: str | Path) -> None:
    """Results append, never overwrite (pre-launch checklist)."""
    path = Path(path)
    if not path.parent.exists():
        raise InvariantViolation(f"results directory does not exist: {path.parent}")
    if not os.access(path.parent, os.W_OK):
        raise InvariantViolation(f"results directory is not writable: {path.parent}")
    if path.exists() and not os.access(path, os.W_OK):
        raise InvariantViolation(f"results file exists but is not writable: {path}")


def assert_no_catalog_in_train_config(cfg: ExperimentConfig) -> None:
    """Guard the schema itself against acquiring a training-time catalog field.

    §5 is a 'Critical': one adapter, evaluated against all three catalogs.
    TrainConfig has no catalog field today; this fails loudly if one is ever
    added, since that change would silently turn the study into a memorization
    measurement.
    """
    offending = [f for f in type(cfg.train).model_fields if "catalog" in f.lower()]
    if offending:
        raise InvariantViolation(
            f"TrainConfig gained catalog field(s) {offending}. Plan §5: never train "
            "per-catalog — train one adapter and evaluate it against all catalogs."
        )


def assert_env_keys(required: Sequence[str]) -> None:
    """Preflight only: credentials present before a long job starts."""
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise InvariantViolation(
            f"missing environment variables: {', '.join(missing)}. "
            "Copy .env.example to .env and fill it in."
        )


def check_all(
    cfg: ExperimentConfig,
    *,
    reference_simulator: SimulatorConfig | None = None,
) -> list[str]:
    """Every check that needs no disk or network. Run at the top of each script.

    Returns the names of the checks that passed, for logging.
    """
    assert_prompt_template_match(cfg)
    assert_teacher_model_pinned(cfg)
    assert_seed_discipline(cfg)
    assert_no_catalog_in_train_config(cfg)
    assert_results_appendable(cfg.paths.results_dir / f"{cfg.name}.jsonl")

    passed = [
        "prompt_template_match",
        "teacher_model_pinned",
        "seed_discipline",
        "no_catalog_in_train_config",
        "results_appendable",
    ]
    if reference_simulator is not None:
        assert_simulator_frozen(cfg, reference_simulator)
        passed.append("simulator_frozen")
    return passed
