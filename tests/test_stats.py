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


def _episodes(fp="f", tasks=("a", "b"), seeds=(0, 1), win=("a", 0), **extra):
    return [
        {
            "config_fingerprint": fp,
            "task_id": t,
            "seed": s,
            "reward": 1.0 if (t, s) == win else 0.0,
            "student_turns": 4,
            **extra,
        }
        for t in tasks
        for s in seeds
    ]


def test_a_free_row_reads_model_catalog_and_threshold_from_the_file_name():
    from pathlib import Path

    from src.stats import free_row

    a = free_row(Path("free-qwen15b-sft-c80.jsonl"), _episodes())
    assert (a["label"], a["catalog"], a["threshold"], a["complete"]) == (
        "qwen15b-sft",
        80,
        None,
        True,
    )
    assert a["task_success"]["mean"] == 0.25 and a["escalation_rate"] == 0.0
    d = free_row(Path("free-qwen15b-sft-esc0.70-c80.jsonl"), _episodes(escalated_turns=1))
    assert (d["label"], d["threshold"], d["escalation_rate"]) == ("qwen15b-sft", 0.7, 0.25)
    assert free_row(Path("harvest-baseline.jsonl"), _episodes()) is None


def test_a_cell_still_filling_reports_a_count_not_a_ci():
    from pathlib import Path

    from src.stats import free_row

    partial = _episodes()[:3]  # task b has one seed, task a two
    row = free_row(Path("free-x-c16.jsonl"), partial)
    assert row["complete"] is False and (row["successes"], row["n_episodes"]) == (1, 3)


def test_a_results_file_with_two_identities_is_refused():
    from pathlib import Path

    from src.stats import free_row

    with pytest.raises(ValueError, match="identities"):
        free_row(Path("free-x-c16.jsonl"), _episodes(fp="f") + _episodes(fp="g"))


def test_the_markdown_puts_stage_a_by_catalog_and_stage_d_by_threshold():
    from pathlib import Path

    from src.stats import free_markdown, free_row

    rows = [
        free_row(Path("free-m-c16.jsonl"), _episodes()),
        free_row(Path("free-m-esc0.50-c80.jsonl"), _episodes(escalated_turns=2)),
    ]
    teacher = {80: success_table(_episodes(win=("b", 1)))}
    md = free_markdown(rows, teacher)
    assert "| Model | c16 | c80 |" in md
    assert "| m | 25.0%" in md and "| teacher |  | 25.0%" in md
    assert "| m | 80 | 0.50 | 50.0% | 25.0%" in md
