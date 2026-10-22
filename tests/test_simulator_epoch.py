"""Simulator epochs (configs/sim/): a new simulator is a new user, so students
under it are compared only with a teacher baseline run under it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import load_config
from src.eval_free import assert_same_harness_as_baseline
from src.invariants import InvariantViolation
from src.stats import V1_SIMULATOR, free_row


def _cfg(path, results: Path):
    cfg = load_config(path)
    return cfg.model_copy(update={"paths": cfg.paths.model_copy(update={"results_dir": results})})


def _log(path: Path, fingerprint: str, smoke: bool = False) -> None:
    rec = {"config_fingerprint": fingerprint, "task_id": "1", "seed": 0, "smoke": smoke}
    path.write_text(json.dumps(rec) + "\n")


def test_v1_base_simulator_matches_the_stats_legacy_constant():
    assert load_config("configs/qwen3b.yaml").simulator.model == V1_SIMULATOR


def test_a_student_under_a_new_simulator_needs_its_own_teacher_baseline(tmp_path):
    v1 = _cfg("configs/qwen3b.yaml", tmp_path)
    g = _cfg("configs/gemma/qwen3b.yaml", tmp_path)
    _log(tmp_path / "harvest-baseline.jsonl", v1.teacher_fingerprint())
    assert assert_same_harness_as_baseline(v1) == v1.teacher_fingerprint()
    with pytest.raises(InvariantViolation, match="teacher baseline under this simulator"):
        assert_same_harness_as_baseline(g)


def test_a_padded_baseline_counts_and_a_smoke_one_does_not(tmp_path):
    g = _cfg("configs/gemma/qwen3b.yaml", tmp_path)
    _log(tmp_path / "harvest-baseline-c80.jsonl", g.padded_baseline_fingerprint(80), smoke=True)
    with pytest.raises(InvariantViolation):
        assert_same_harness_as_baseline(g)
    _log(tmp_path / "harvest-baseline-c80.jsonl", g.padded_baseline_fingerprint(80))
    # the identity half is the teacher fingerprint, exactly as for the v1 cells
    assert assert_same_harness_as_baseline(g) == g.teacher_fingerprint()


def _episodes(sim=None):
    return [
        {"config_fingerprint": "f", "task_id": t, "seed": s, "reward": 0.0, "student_turns": 3}
        | ({"simulator": sim} if sim else {})
        for t in ("a", "b")
        for s in (0, 1)
    ]


def test_free_rows_carry_their_simulator_and_old_records_mean_v1():
    old = free_row(Path("free-qwen3b-sft-c80.jsonl"), _episodes())
    new = free_row(Path("free-qwen3b-gemma-sft-c80.jsonl"), _episodes("openrouter/google/x"))
    assert old["simulator"] == V1_SIMULATOR and new["simulator"] == "openrouter/google/x"


def test_one_results_file_never_mixes_simulators():
    mixed = _episodes()[:2] + _episodes("openrouter/google/x")[2:]
    with pytest.raises(ValueError, match="simulators"):
        free_row(Path("free-qwen3b-sft-c80.jsonl"), mixed)
