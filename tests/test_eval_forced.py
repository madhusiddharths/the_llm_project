"""Teacher-forced replay: prompts come only from teacher messages, and nothing
is silently skipped. The teacher must agree with itself on every step."""

from __future__ import annotations

import pytest

from src.build_sft import assert_train_only, episode_record
from src.eval_forced import eval_steps, score_steps, teacher_completions
from src.invariants import InvariantViolation
from src.metrics import summarize
from src.prompts import _GOLDEN, serialize_state
from src.trajectories import Episode


def _episode(task_id="7", rep=0):
    return Episode(
        task_id=task_id,
        rep=rep,
        temperature=0.0,
        reward=1.0,
        trajectory_file="f",
        messages=tuple(_GOLDEN["messages"]),
    )


def _steps():
    return eval_steps([_episode()], system=_GOLDEN["system"], tools=_GOLDEN["tools"])


def test_step_ids_are_task_rep_and_message_index():
    assert [s.step_id for s in _steps()] == ["7:0:2", "7:0:4"]


def test_every_prompt_is_rendered_from_teacher_messages_only():
    msgs = _GOLDEN["messages"]
    for s in _steps():
        assert s.prompt == serialize_state(
            system=_GOLDEN["system"], tools=_GOLDEN["tools"], messages=msgs[: s.index]
        )


def test_the_teacher_agrees_with_itself_on_every_step():
    """ACCEPTANCE for V1-05: validate the evaluator against the teacher."""
    steps = _steps()
    scored = score_steps(steps, teacher_completions(steps), ["cancel_order"])
    rows = [r for rs in scored.values() for r in rs]
    s = summarize(rows)
    assert s["step_agreement"] == 1.0 and s["decision_type_accuracy"] == 1.0


def test_a_missing_completion_is_an_error_not_a_skipped_step():
    steps = _steps()
    completions = teacher_completions(steps)
    completions.pop(steps[-1].step_id)
    with pytest.raises(ValueError, match="no completion"):
        score_steps(steps, completions, ["cancel_order"])


# --- build_sft ---------------------------------------------------------------


def test_training_data_from_an_eval_task_is_refused():
    with pytest.raises(InvariantViolation, match="eval-split"):
        assert_train_only([_episode("7")], train_ids=["1"], eval_ids=["7"])


def test_training_data_from_outside_both_splits_is_refused():
    with pytest.raises(InvariantViolation, match="outside the train split"):
        assert_train_only([_episode("9")], train_ids=["1"], eval_ids=["7"])


def test_an_sft_record_carries_the_segments_and_its_decision_count():
    rec = episode_record(_episode(), system=_GOLDEN["system"], tools=_GOLDEN["tools"])
    assert rec["n_decisions"] == 2
    assert sum(train for _, train in rec["segments"]) == 2
    assert rec["chars"] == sum(len(text) for text, _ in rec["segments"])
