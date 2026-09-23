"""Each test here corresponds to a named failure mode in the plan."""

from __future__ import annotations

import pytest

from src import prompts
from src.config import load_config
from src.invariants import (
    InvariantViolation,
    assert_catalog_hash,
    assert_env_keys,
    assert_judge_frozen,
    assert_judge_not_teacher,
    assert_no_catalog_in_train_config,
    assert_prompt_template_match,
    assert_results_appendable,
    assert_seed_discipline,
    assert_simulator_frozen,
    assert_split_disjoint,
    assert_teacher_model_pinned,
    check_all,
)

REAL = "configs/qwen05b.yaml"


# --- the acceptance test named in division-of-labor.md ----------------------


def test_mismatched_prompt_hash_aborts_the_run(monkeypatch):
    """ACCEPTANCE: "Deliberately mismatch a prompt hash, run aborts."."""
    cfg = load_config(REAL)
    assert_prompt_template_match(cfg)  # baseline: currently agrees

    monkeypatch.setattr(prompts, "TOOLS_TEMPLATE", prompts.TOOLS_TEMPLATE + " ")
    with pytest.raises(InvariantViolation, match="prompt template hash mismatch"):
        assert_prompt_template_match(cfg)


def test_serializer_change_alone_is_caught(monkeypatch):
    """The hash covers serializer OUTPUT, not just the template string.

    A change to serialize_state that leaves TOOLS_TEMPLATE untouched still
    changes every prompt the model sees, and must still abort.
    """
    cfg = load_config(REAL)
    original = prompts.serialize_state

    def altered(*, system, tools, messages, tool_format="compact"):
        text = original(system=system, tools=tools, messages=messages, tool_format=tool_format)
        return text.replace("\n", "\n\n")

    monkeypatch.setattr(prompts, "serialize_state", altered)
    with pytest.raises(InvariantViolation, match="prompt template hash mismatch"):
        assert_prompt_template_match(cfg)


def test_configs_disagreeing_with_each_other_abort():
    cfg = load_config(REAL)
    drifted = cfg.model_copy(update={"prompt_template_hash": "0" * 64})
    with pytest.raises(InvariantViolation):
        assert_prompt_template_match(cfg, drifted)


def test_check_all_aborts_on_prompt_drift(monkeypatch):
    """The whole-run entry point must fail, not just the individual assertion."""
    cfg = load_config(REAL)
    monkeypatch.setattr(prompts, "TOOLS_TEMPLATE", "totally different {tools}")
    with pytest.raises(InvariantViolation):
        check_all(cfg)


# --- teacher must be frozen for the whole project ---------------------------


@pytest.mark.parametrize(
    "alias", ["gemini-flash-latest", "gemini-3-flash-preview", "GPT-OSS-Latest"]
)
def test_floating_teacher_alias_aborts(alias):
    """A repointed teacher invalidates every comparison, and looks fine doing it."""
    cfg = load_config(REAL)
    drifting = cfg.model_copy(update={"teacher": cfg.teacher.model_copy(update={"model": alias})})
    with pytest.raises(InvariantViolation, match="floating alias"):
        assert_teacher_model_pinned(drifting)


def test_pinned_teacher_passes():
    assert_teacher_model_pinned(load_config(REAL))  # does not raise


def test_check_all_rejects_a_floating_teacher():
    cfg = load_config(REAL)
    drifting = cfg.model_copy(
        update={"teacher": cfg.teacher.model_copy(update={"model": "gemini-flash-latest"})}
    )
    with pytest.raises(InvariantViolation):
        check_all(drifting)


# --- split integrity (plan §6: split by task ID, never by step) -------------


def test_overlapping_split_aborts():
    with pytest.raises(InvariantViolation, match="both train and eval"):
        assert_split_disjoint(["t1", "t2"], ["t2", "t3"])


def test_disjoint_split_passes():
    assert_split_disjoint(["t1", "t2"], ["t3"])  # does not raise


def test_empty_split_aborts():
    with pytest.raises(InvariantViolation, match="non-empty"):
        assert_split_disjoint(["t1"], [])


# --- simulator frozen (plan §7 constraint 2) --------------------------------


def test_simulator_drift_aborts():
    cfg = load_config(REAL)
    reference = cfg.simulator.model_copy(update={"temperature": 0.9})
    with pytest.raises(InvariantViolation, match="simulator config drifted"):
        assert_simulator_frozen(cfg, reference)


def test_simulator_drift_names_the_offending_field():
    cfg = load_config(REAL)
    reference = cfg.simulator.model_copy(update={"seed": 999})
    with pytest.raises(InvariantViolation, match="seed"):
        assert_simulator_frozen(cfg, reference)


def test_identical_simulator_passes():
    cfg = load_config(REAL)
    assert_simulator_frozen(cfg, cfg.simulator)  # does not raise


# --- judge frozen and independent (the 2026-09-10 harvest failure) ----------


def test_judge_drift_aborts():
    cfg = load_config(REAL)
    reference = cfg.judge.model_copy(update={"temperature": 0.9})
    with pytest.raises(InvariantViolation, match="judge config drifted"):
        assert_judge_frozen(cfg, reference)


def test_judge_drift_names_the_offending_field():
    cfg = load_config(REAL)
    reference = cfg.judge.model_copy(update={"model": "openrouter/some/other-model"})
    with pytest.raises(InvariantViolation, match="model"):
        assert_judge_frozen(cfg, reference)


def test_identical_judge_passes():
    cfg = load_config(REAL)
    assert_judge_frozen(cfg, cfg.judge)  # does not raise


def test_teacher_may_not_grade_itself():
    """Rejection sampling filters the training set; the filter must be independent."""
    cfg = load_config(REAL)
    selfgrading = cfg.model_copy(
        update={"judge": cfg.judge.model_copy(update={"model": f"openrouter/{cfg.teacher.model}"})}
    )
    with pytest.raises(InvariantViolation, match="cannot grade its own"):
        assert_judge_not_teacher(selfgrading)


def test_self_grading_is_caught_through_the_provider_prefix():
    """teacher.model is bare, judge.model carries 'openrouter/'. Same model either way."""
    cfg = load_config(REAL)
    selfgrading = cfg.model_copy(
        update={"judge": cfg.judge.model_copy(update={"model": cfg.teacher.model})}
    )
    with pytest.raises(InvariantViolation, match="cannot grade its own"):
        assert_judge_not_teacher(selfgrading)


def test_distinct_judge_passes():
    assert assert_judge_not_teacher(load_config(REAL)) is None


def test_real_config_does_not_leave_the_judge_on_the_tau2_default():
    """tau2 hardcodes gpt-4.1 and we hold no OpenAI key. Anything OpenAI-shaped
    here means 40 of the 114 retail tasks fail at scoring time, as they did on
    2026-09-10."""
    model = load_config(REAL).judge.model
    assert model.startswith("openrouter/"), model
    assert "gpt" not in model.lower(), model


# --- seed discipline (plan §10: 5 seeds minimum) ----------------------------


def test_too_few_seeds_aborts_a_real_run():
    cfg = load_config(REAL)
    thin = cfg.model_copy(update={"eval": cfg.eval.model_copy(update={"seeds": [0, 1]})})
    with pytest.raises(InvariantViolation, match="confidence intervals"):
        assert_seed_discipline(thin)


def test_smoke_is_exempt_from_the_seed_minimum():
    assert_seed_discipline(load_config(REAL, smoke=True))  # does not raise


# --- catalog integrity (plan §5) --------------------------------------------


def test_unpinned_catalog_aborts(tmp_path):
    cfg = load_config(REAL)
    catalog = tmp_path / "tools_15.json"
    catalog.write_text('{"tools": []}')
    with pytest.raises(InvariantViolation, match="not pinned"):
        assert_catalog_hash(cfg, 15, catalog)


def test_catalog_hash_mismatch_aborts(tmp_path):
    cfg = load_config(REAL)
    catalog = tmp_path / "tools_15.json"
    catalog.write_text('{"tools": []}')
    pinned = cfg.model_copy(
        update={"eval": cfg.eval.model_copy(update={"catalog_hashes": {15: "0" * 64}})}
    )
    with pytest.raises(InvariantViolation, match="hash mismatch"):
        assert_catalog_hash(pinned, 15, catalog)


def test_matching_catalog_hash_passes(tmp_path):
    from src.hashing import hash_file

    cfg = load_config(REAL)
    catalog = tmp_path / "tools_15.json"
    catalog.write_text('{"tools": []}')
    pinned = cfg.model_copy(
        update={"eval": cfg.eval.model_copy(update={"catalog_hashes": {15: hash_file(catalog)}})}
    )
    assert assert_catalog_hash(pinned, 15, catalog) is None


def test_missing_catalog_aborts(tmp_path):
    with pytest.raises(InvariantViolation, match="not found"):
        assert_catalog_hash(load_config(REAL), 15, tmp_path / "nope.json")


def test_train_config_may_not_gain_a_catalog_field():
    assert assert_no_catalog_in_train_config(load_config(REAL)) is None


# --- results path -----------------------------------------------------------


def test_missing_results_dir_aborts(tmp_path):
    with pytest.raises(InvariantViolation, match="does not exist"):
        assert_results_appendable(tmp_path / "nope" / "out.jsonl")


def test_writable_results_dir_passes(tmp_path):
    assert assert_results_appendable(tmp_path / "out.jsonl") is None


# --- credentials (preflight) -------------------------------------------------


def test_missing_env_keys_abort(monkeypatch):
    monkeypatch.delenv("SOME_ABSENT_KEY", raising=False)
    with pytest.raises(InvariantViolation, match="SOME_ABSENT_KEY"):
        assert_env_keys(["SOME_ABSENT_KEY"])


def test_present_env_key_passes(monkeypatch):
    monkeypatch.setenv("SOME_PRESENT_KEY", "x")
    assert assert_env_keys(["SOME_PRESENT_KEY"]) is None


def test_clean_config_passes_every_check():
    assert len(check_all(load_config(REAL))) == 6
