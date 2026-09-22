"""pass^k and CIs, checked against hand-computed values."""

from __future__ import annotations

import pytest

from src.stats import mean_ci, pass_hat_k, success_table


def test_pass_hat_k_matches_the_combinatorial_definition():
    outcomes = {"a": [True, True, False], "b": [True, False, False]}
    # a: C(2,1)/C(3,1)=2/3, b: 1/3 -> pass^1 = 0.5
    assert pass_hat_k(outcomes, 1) == 0.5
    # a: C(2,2)/C(3,2)=1/3, b: 0 -> pass^2 = 1/6
    assert pass_hat_k(outcomes, 2) == pytest.approx(1 / 6, abs=1e-4)
    # nobody succeeds three times out of three
    assert pass_hat_k(outcomes, 3) == 0.0


def test_pass_hat_k_refuses_too_few_trials():
    with pytest.raises(ValueError, match="at least 3"):
        pass_hat_k({"a": [True, True]}, 3)


def test_ci_uses_student_t_at_small_n():
    ci = mean_ci([0.4, 0.5, 0.6])
    # sd = 0.1, t(2) = 4.303, half = 4.303 * 0.1 / sqrt(3) = 0.2484
    assert ci["mean"] == 0.5
    assert ci["ci95_low"] == pytest.approx(0.2516, abs=1e-4)
    assert ci["ci95_high"] == pytest.approx(0.7484, abs=1e-4)


def test_success_table_refuses_unequal_trials():
    recs = [
        {"task_id": "a", "seed": 0, "reward": 1.0},
        {"task_id": "a", "seed": 1, "reward": 0.0},
        {"task_id": "b", "seed": 0, "reward": 1.0},
    ]
    with pytest.raises(ValueError, match="unequal"):
        success_table(recs)


def test_success_table_on_a_tiny_grid():
    recs = [
        {"task_id": t, "seed": s, "reward": r}
        for t, rs in {"a": [1.0, 1.0], "b": [1.0, 0.0]}.items()
        for s, r in enumerate(rs)
    ]
    table = success_table(recs)
    assert table["per_seed"] == {0: 1.0, 1: 0.5}
    assert table["pass^2"] == 0.5
    assert (table["tasks_solved_any_seed"], table["tasks_solved_every_seed"]) == (2, 1)
