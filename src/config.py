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
    "eval": {"n_tasks": 2, "seeds": [0], "catalog_sizes": [16]},
    "escalation": {"thresholds": [0.7]},
}
# Smoke overrides for optional sections, applied only when the config sets the
# section: merging them into a config without it would create a half-filled one.
SMOKE_OVERRIDES_IF_SET: dict[str, Any] = {
    "dpo": {"epochs": 1, "max_steps": 1, "batch_size": 1, "grad_accum": 1},
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


class JudgeConfig(_Frozen):
    """tau2's NL-assertion grader. Frozen for the same reason the simulator is.

    tau2 defaults this to gpt-4.1 and we have no OpenAI key, so it must be set
    explicitly or 40 of the 114 retail tasks fail at scoring time. See src/judge.py
    for why the judge is kept rather than switched off, and why it may not be the
    teacher. invariants.assert_judge_frozen and assert_judge_not_teacher enforce both.
    """

    model: str
    temperature: float


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


# --- v2/v3 extension sections (plan X10) --------------------------------------
# Every one is optional and absent from configs/base.yaml. fingerprint() drops
# the ones left unset, so no v1 cell identity moves (tests/test_config.py pins
# the v1 fingerprints). They are set only by the configs under configs/v2/.


class DPOConfig(_Frozen):
    """Stage E3 (plan X5). Starting points to tune, not settled values."""

    beta: float
    learning_rate: float
    epochs: int
    batch_size: int  # pairs per forward group; kept at 1 (one sequence per pass)
    grad_accum: int  # pairs per optimizer step = batch_size * grad_accum
    # Which completions the rejected side came from (make_pairs.py manifest).
    pair_source: str
    sampling_temperature: float
    # "continue_sft_adapter": the policy is the SFT LoRA itself, trained further;
    # the reference is that adapter frozen at the start (log-probs precomputed).
    # The result is again one LoRA over the base model, so every eval path takes
    # it unchanged. See src/train_dpo.py.
    init: Literal["continue_sft_adapter"]
    max_steps: int | None = None


class GRPOConfig(_Frozen):
    """Stage G/H (plan X7, X8). Schema only until train_grpo.py lands."""

    level: Literal["step", "episode"]
    group_size: int
    reward_weights: dict[str, float]
    kl_coef: float
    learning_rate: float
    max_steps: int


class ServingConfig(_Frozen):
    """Stage F (plan X6). Schema only until serve_bench.py lands."""

    vllm_version: str
    concurrency_levels: list[int]
    prefix_caching: bool = True
    guided_decoding: bool = False
    quantization: Literal["none", "awq", "gptq"] = "none"


class AblationConfig(_Frozen):
    """Stage E1 data-scaling ablation (plan X5, resolved 2026-09-22)."""

    fractions: list[float]
    # The teacher tasks with at least one successful episode (46), subset by
    # task id. Public data, if any were ever mixed in, stays at 100%.
    denominator: Literal["teacher_tasks"]
    subset_seed: int

    @field_validator("fractions")
    @classmethod
    def _fractions_in_range(cls, v: list[float]) -> list[float]:
        if not v or any(not 0 < f <= 1 for f in v):
            raise ValueError(f"ablation.fractions must lie in (0, 1]: {v}")
        return v


# Dropped from fingerprint() while None, so adding them moved no v1 identity.
EXTENSION_FIELDS = frozenset({"dpo", "grpo", "serving", "ablation", "sim_tag"})


class PathsConfig(_Frozen):
    results_dir: Path = Path("results")
    trajectories_dir: Path = Path("data/trajectories")
    catalogs_dir: Path = Path("data/catalogs")


class ExperimentConfig(_Frozen):
    name: str
    # Pinned in base.yaml. Live value comes from prompts.template_hash().
    prompt_template_hash: str
    # The serializer hash that was live when the teacher harvest was recorded.
    # Only teacher_fingerprint() reads it; see there for why it must not float.
    harvest_prompt_template_hash: str
    # sha256 of data/catalogs/system_prompt.txt (tau2's instructions + retail
    # policy), which train and eval both render. See src/catalogs.py.
    system_prompt_hash: str | None = None
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
    judge: JudgeConfig
    eval: EvalConfig
    escalation: EscalationConfig
    paths: PathsConfig = Field(default_factory=PathsConfig)

    # A short label for a simulator/judge other than base.yaml's, set by
    # configs/sim/<tag>.yaml. It names the teacher summary files so the two
    # simulator epochs never overwrite each other. None = the v1 simulator.
    sim_tag: str | None = None
    dpo: DPOConfig | None = None
    grpo: GRPOConfig | None = None
    serving: ServingConfig | None = None
    ablation: AblationConfig | None = None

    def fingerprint(self) -> str:
        """Identity of this experiment cell, stamped on every result record.

        Paths are excluded: where results happen to be written is not part of
        what was run. `smoke` is included, so scaffold and smoke records can
        never be mistaken for a real run's numbers. Extension sections are
        included only when set (plan X10), so the v1 identities did not move
        when the schema grew.
        """
        unset = {f for f in EXTENSION_FIELDS if getattr(self, f) is None}
        payload = self.model_dump(mode="json", exclude={"paths", *unset})
        return hash_obj(payload)

    def method(self) -> str:
        """Which post-training branch this config describes (plan X4, X10)."""
        if self.dpo is not None:
            return "dpo"
        if self.grpo is not None:
            return "grpo"
        return "sft"

    def short_fingerprint(self) -> str:
        return short(self.fingerprint())

    def teacher_fingerprint(self) -> str:
        """Identity of the harvest, which does not depend on the student.

        The harvest records what the TEACHER did. Keying its resume log to the
        full config would mean switching from qwen05b to qwen15b re-harvests six
        days of episodes that are already on disk and still valid.

        The same logic applies to the student's serializer. tau2 built the
        teacher's prompts, not src/prompts.py, so a serializer change must not
        re-key the harvest. It would, if this read the live
        prompt_template_hash: the harvest log and the trajectory directory
        (data/trajectories/<name>/<short fingerprint>/) are both keyed to this
        value, and 162 recorded episodes would silently stop matching. So the
        payload carries the hash that was live at harvest time, pinned in
        base.yaml as harvest_prompt_template_hash. The payload keeps its original
        shape, so the fingerprint is byte-identical to the one the harvest was
        recorded under; tests/test_config.py pins that value.
        """
        payload = self.model_dump(
            mode="json",
            include={
                "teacher",
                "split",
                "harvest",
                "simulator",
                # The judge sets the reward that gets logged, so two harvests
                # graded by different judges are not the same harvest.
                "judge",
            },
        )
        payload["prompt_template_hash"] = self.harvest_prompt_template_hash
        return hash_obj(payload)

    def padded_baseline_fingerprint(self, catalog: int) -> str:
        """Identity of a teacher baseline on a padded catalog (D7: 40, 80).

        The teacher fingerprint plus the catalog's pinned hash, so the native
        baseline's log is never touched. harvest.py writes under it and stats.py
        reads under it; both must call this.
        """
        return hash_obj(
            {
                "teacher": self.teacher_fingerprint(),
                "catalog": catalog,
                "catalog_hash": self.eval.catalog_hashes[catalog],
            }
        )


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
    """Read YAML, resolving an optional `extends:` chain relative to the file.

    `extends` is one path or a list; a list merges left to right, then this
    file on top. A list lets a model config and a shared overlay (a simulator
    epoch, a v2 section) combine without copying the overlay into every file.
    """
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
    refs = parent_ref if isinstance(parent_ref, list) else [parent_ref]
    merged: dict[str, Any] = {}
    for ref in refs:
        merged = _deep_merge(merged, _load_raw(path.parent / str(ref), _seen | {path}))
    return _deep_merge(merged, raw)


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
        raw = _deep_merge(raw, {k: v for k, v in SMOKE_OVERRIDES_IF_SET.items() if k in raw})
        raw["smoke"] = True
    if seed is not None:
        raw["seed"] = seed
    return ExperimentConfig(**raw)
