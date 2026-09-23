"""The shared episode loader: which episodes count, and what counts as a decision.

Every training example and every teacher-forced step comes through here, so an
off-by-one or a leaked record would reach both train and eval at once.
"""

from __future__ import annotations

import json

import pytest

from src.trajectories import (
    DEGENERATE_USER_CHARS,
    TAU2_GREETING,
    Episode,
    decision_steps,
    has_degenerate_user_turn,
    load_episodes,
    parse_action,
)

FP = "f" * 64


def _greeting():
    return {"role": "assistant", "content": TAU2_GREETING, "tool_calls": None}


def _call(name, **arguments):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "name": name, "arguments": arguments}],
    }


def _messages():
    return [
        _greeting(),
        {"role": "user", "content": "cancel order #W1"},
        {"role": "assistant", "content": "What is your email?", "tool_calls": None},
        {"role": "user", "content": "a@b.c"},
        _call("find_user_id_by_email", email="a@b.c"),
        {"role": "tool", "id": "c1", "content": "user_1", "error": False},
        {"role": "assistant", "content": "Found you.", "tool_calls": None},
        {"role": "user", "content": "###STOP###"},
    ]


def test_the_hardcoded_greeting_is_context_not_a_decision():
    steps = decision_steps(_messages())
    assert [s.index for s in steps] == [2, 4, 6]
    assert steps[0].context[0]["content"] == TAU2_GREETING


def test_each_step_sees_exactly_the_messages_before_it():
    steps = decision_steps(_messages())
    assert len(steps[1].context) == 4
    assert steps[1].action.kind == "tool_call"
    assert steps[1].action.calls[0].name == "find_user_id_by_email"
    assert steps[1].action.calls[0].arguments == {"email": "a@b.c"}


def test_a_missing_greeting_fails_loudly_instead_of_shifting_every_index():
    with pytest.raises(ValueError, match="greeting"):
        decision_steps(_messages()[1:])


def test_a_message_with_text_and_a_tool_call_is_the_tool_call():
    """tau2 executes the call; the stray text is not a second action."""
    msg = _call("get_order_details", order_id="#W1")
    msg["content"] = "Let me check that."
    action = parse_action(msg)
    assert action.kind == "tool_call" and action.text == ""


def test_an_empty_assistant_message_is_an_error_not_an_empty_reply():
    with pytest.raises(ValueError, match="empty"):
        parse_action({"role": "assistant", "content": "  ", "tool_calls": None, "turn_idx": 3})


# --- load_episodes ------------------------------------------------------------


def _write_batch(path, sims):
    path.write_text(json.dumps({"simulations": sims}))


def _sim(task_id, reward, messages=None):
    return {
        "task_id": task_id,
        "reward_info": None if reward is None else {"reward": reward},
        "messages": messages if messages is not None else _messages(),
    }


def _write_log(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _row(task_id, rep, reward, batch, fingerprint=FP, smoke=False):
    return {
        "config_fingerprint": fingerprint,
        "task_id": task_id,
        "seed": rep,
        "smoke": smoke,
        "reward": reward,
        "temperature": 0.0,
        "trajectory_file": str(batch),
    }


def test_only_successful_episodes_under_this_fingerprint_are_loaded(tmp_path):
    batch = tmp_path / "b0.json"
    _write_batch(batch, [_sim("1", 1.0), _sim("2", 0.0), _sim("3", None), _sim("4", 1.0)])
    log = tmp_path / "log.jsonl"
    _write_log(
        log,
        [
            _row("1", 0, 1.0, batch),
            _row("2", 0, 0.0, batch),  # failed: rejection sampling drops it
            _row("4", 0, 1.0, batch, fingerprint="0" * 64),  # older config
            _row("4", 1, 1.0, batch, smoke=True),  # smoke record
        ],
    )
    episodes = load_episodes(log, fingerprint=FP)
    assert [(e.task_id, e.rep) for e in episodes] == [("1", 0)]

    everything = load_episodes(log, fingerprint=FP, successful_only=False)
    assert {e.task_id for e in everything} == {"1", "2"}


def test_a_logged_episode_missing_from_its_batch_file_is_an_error(tmp_path):
    batch = tmp_path / "b0.json"
    _write_batch(batch, [_sim("1", None)])  # only an infrastructure error
    log = tmp_path / "log.jsonl"
    _write_log(log, [_row("1", 0, 1.0, batch)])
    with pytest.raises(ValueError, match="no scored simulation"):
        load_episodes(log, fingerprint=FP)


def test_two_scored_simulations_for_one_task_is_an_error(tmp_path):
    batch = tmp_path / "b0.json"
    _write_batch(batch, [_sim("1", 1.0), _sim("1", 0.0)])
    log = tmp_path / "log.jsonl"
    _write_log(log, [_row("1", 0, 1.0, batch)])
    with pytest.raises(ValueError, match="two scored"):
        load_episodes(log, fingerprint=FP)


def test_a_runaway_simulator_turn_marks_the_episode_degenerate():
    def ep(user_text):
        msgs = _messages()
        msgs[3] = {"role": "user", "content": user_text}
        return Episode("1", 0, 0.0, 1.0, "f", tuple(msgs))

    assert not has_degenerate_user_turn(ep("a@b.c"))
    assert has_degenerate_user_turn(ep("I'll proceed. " * (DEGENERATE_USER_CHARS // 10)))
