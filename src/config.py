"""Experiment config: schema, `extends` merge, smoke overrides, fingerprint.

Two structural guarantees live here, both from the plan:

1.  **Catalog is an eval-time axis, never a training field.** §5 is explicit:
    "one adapter, evaluated against all three catalogs. Never train per-catalog
    — that measures memorization, not degradation." TrainConfig therefore has no
    catalog field, so the mistake cannot be expressed in YAML at all.

2.  **`--smoke` is a config-load override, not a branch.** SMOKE_OVERRIDES is
    applied once, here, when the config loads. No `if smoke:` appears in any
    logic path, so the smoke and real paths cannot drift apart — which is the
    mechanism division-of-labor.md says makes the whole hand-off work.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.hashing import hash_obj, short

# Applied on load when --smoke is passed. One place, both paths.
# Target: every script finishes in under two minutes (division-of-labor.md).
SMOKE_OVERRIDES: dict[str, Any] = {
    # A real retail episode runs ~194s, so "tiny inputs" has to mean a shorter
    # episode, not just fewer of them: one task, capped at a couple of turns.
    "harvest": {"n_tasks": 1, "samples_per_task": 1},
    "simulator": {"max_turns": 2},
    "train": {"epochs": 1, "max_steps": 1, "batch_size": 1, "grad_accum": 1},
    "eval": {"n_tasks": 2, "seeds": [0], "catalog_sizes": [15]},
    "escalation": {"thresholds": [0.7]},
}


class _Frozen(BaseModel):
    # extra="forbid" turns a typo'd YAML key into an immediate error instead of
    # a silently ignored setting. frozen=True stops mid-run mutation.
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelSpec(_Frozen):
    base_model: str
    params_b: float
    adapter_repo: str | None = None


class TeacherConfig(_Frozen):
    """The teacher API and the rate ceiling the harvest must pace against.

    tokens_per_minute is the real constraint on a free tier, not requests: one
    retail agent turn carries the whole tool catalog, so a single call can be
    several thousand tokens and the TPM ceiling binds long before the RPM one.
    """

    provider: Literal["groq", "openrouter", "google", "nvidia_nim"]
    model: str
    temperatures: list[float]
    max_tokens: int = 1024
    requests_per_minute: int = 30
    requests_per_day: int | None = None
    tokens_per_minute: int | None = None  # None when the provider meters requests only
    avg_tokens_per_call: int  # measured, for quota arithmetic before a launch

    def calls_per_minute(self) -> float:
        """What the ceilings actually allow: whichever of TPM or RPM binds first.

        Groq taught us this the hard way — a headline 1,000 requests/day meant 48
        in practice, because the token cap ran out first.
        """
        if self.tokens_per_minute is None:
            return float(self.requests_per_minute)
        return min(self.tokens_per_minute / self.avg_tokens_per_call, self.requests_per_minute)

    def episodes_per_day(self, steps_per_episode: int = 8) -> float | None:
        """Episodes the daily request cap permits. None when there is no cap."""
        if self.requests_per_day is None:
            return None
        return self.requests_per_day / steps_per_episode


class SplitConfig(_Frozen):
    """Split by task ID, never by step (§6). Sizes are asserted in invariants."""

    n_train: int
    n_eval: int
    split_seed: int


class HarvestConfig(_Frozen):
    n_tasks: int
    samples_per_task: int
    rejection_sampling: bool = True


class TrainConfig(_Frozen):
    """No catalog field. See module docstring, guarantee 1."""

    lora_r: int
    lora_alpha: int
    lora_dropout: float
    learning_rate: float
    epochs: int
    batch_size: int
    grad_accum: int
    max_seq_len: int
    max_steps: int | None = None


class SimulatorConfig(_Frozen):
    """Frozen across every config, or the comparison is meaningless (§7).

    The in-house user simulator is only methodologically defensible because it is
    held constant. invariants.assert_simulator_frozen enforces that.
    """

    model: str
    temperature: float
    seed: int
    max_turns: int


class EvalConfig(_Frozen):
    catalog_sizes: list[int]
    n_tasks: int
    seeds: list[int]
    mode: Literal["forced", "free", "both"] = "both"
    # sha256 per catalog size, pinned once the catalogs are built in week 2.
    # Empty until then; preflight warns about any unpinned catalog on disk.
    catalog_hashes: dict[int, str] = Field(default_factory=dict)

    @field_validator("seeds")
    @classmethod
    def _seeds_unique(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("eval.seeds must not be empty")
        if len(set(v)) != len(v):
            raise ValueError(f"eval.seeds contains duplicates: {v}")
        return v


class EscalationConfig(_Frozen):
    enabled: bool = False
    thresholds: list[float] = Field(default_factory=list)


class PathsConfig(_Frozen):
    results_dir: Path = Path("results")
    trajectories_dir: Path = Path("data/trajectories")
    catalogs_dir: Path = Path("data/catalogs")


class ExperimentConfig(_Frozen):
    name: str
    # Pinned in base.yaml. Live value comes from prompts.template_hash().
    prompt_template_hash: str
    # Required, no default — "seed set explicitly, not left to default" is a
    # pre-launch checklist item, so the schema refuses a config that omits it.
    seed: int
    smoke: bool = False

    model: ModelSpec
    teacher: TeacherConfig
    split: SplitConfig
    harvest: HarvestConfig
    train: TrainConfig
    simulator: SimulatorConfig
    eval: EvalConfig
    escalation: EscalationConfig
    paths: PathsConfig = Field(default_factory=PathsConfig)

    def fingerprint(self) -> str:
        """Identity of this experiment cell, stamped on every result record.

        Paths are excluded: where results happen to be written is not part of
        what was run. `smoke` is included, so scaffold and smoke records can
        never be mistaken for a real run's numbers.
        """
        payload = self.model_dump(mode="json", exclude={"paths"})
        return hash_obj(payload)

    def short_fingerprint(self) -> str:
        return short(self.fingerprint())

    def teacher_fingerprint(self) -> str:
        """Identity of the harvest, which does not depend on the student.

        The harvest records what the TEACHER did. Keying its resume log to the
        full config would mean switching from qwen05b to qwen15b re-harvests six
        days of episodes that are already on disk and still valid.
        """
        payload = self.model_dump(
            mode="json",
            include={"prompt_template_hash", "teacher", "split", "harvest", "simulator"},
        )
        return hash_obj(payload)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Overlay wins. Nested dicts merge; lists and scalars replace wholesale."""
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _load_raw(path: Path, _seen: frozenset[Path] = frozenset()) -> dict[str, Any]:
    """Read YAML, resolving an optional `extends:` chain relative to the file."""
    path = path.resolve()
    if path in _seen:
        chain = " -> ".join(str(p) for p in (*_seen, path))
        raise ValueError(f"circular `extends` in config chain: {chain}")

    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise TypeError(f"{path} must contain a YAML mapping, got {type(raw).__name__}")

    parent_ref = raw.pop("extends", None)
    if parent_ref is None:
        return raw
    parent = _load_raw(path.parent / str(parent_ref), _seen | {path})
    return _deep_merge(parent, raw)


def load_config(
    path: str | Path,
    *,
    smoke: bool = False,
    seed: int | None = None,
) -> ExperimentConfig:
    """Load, merge `extends`, apply smoke overrides, validate.

    This is the only entry point. Nothing else parses config YAML.
    """
    raw = _load_raw(Path(path))
    if smoke:
        raw = _deep_merge(raw, SMOKE_OVERRIDES)
        raw["smoke"] = True
    if seed is not None:
        raw["seed"] = seed
    return ExperimentConfig(**raw)
