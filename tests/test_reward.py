"""G1 step reward (plan X7): hand-built valid, invalid, equivalent and
non-equivalent cases on a fake DB, plus the determinism invariant."""

from __future__ import annotations

import json

import pytest

from src.invariants import InvariantViolation, assert_reward_deterministic
from src.prompts import IM_END
from src.reward import DEFAULT_WEIGHTS, check_weights, schema_ok, step_reward
from src.trajectories import Action, ToolCall

ORDER_ID = {"type": "string"}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_order",
            "parameters": {
                "type": "object",
                "properties": {"order_id": ORDER_ID},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_order",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": ORDER_ID,
                    "item_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["order_id"],
            },
        },
    },
]
CONTEXT = (
    {"role": "assistant", "content": "Hi! How can I help you today?"},
    {"role": "user", "content": "cancel W1"},
)


EXECUTED: list[list[str]] = []  # every action fake_run was asked to run


def fake_run(prefix, calls):
    """A tiny deterministic environment; ids tolerate a leading '#'."""
    EXECUTED.append([c.name for c in calls])
    db = {"W1": "pending", "W2": "delivered"}

    def call(c):
        oid = str(c.arguments.get("order_id", "")).lstrip("#")
        if c.name == "get_order":
            return f"{oid}:{db[oid]}" if oid in db else "Error: not found"
        if c.name == "cancel_order":
            if db.get(oid) != "pending":
                return "Error: cannot cancel"
            db[oid] = "cancelled"
            return "ok"
        return f"Error: Tool '{c.name}' not found"

    for c in prefix:
        call(c)
    return [call(c) for c in calls], json.dumps(db, sort_keys=True)


def _call(name, **args):
    return Action(kind="tool_call", calls=(ToolCall(name=name, arguments=args),))


def _completion(name, **args):
    return (
        "<tool_call>\n" + json.dumps({"name": name, "arguments": args}) + "\n</tool_call>" + IM_END
    )


def _reward(reference, completion):
    return step_reward(reference, completion, CONTEXT, TOOLS, fake_run)


CANCEL = _call("cancel_order", order_id="W1")


def test_the_teachers_own_call_scores_one():
    r = _reward(CANCEL, _completion("cancel_order", order_id="W1"))
    assert r.total == 1.0 and r.components["execution"] == 1.0 and r.reason == ""


def test_the_same_effect_in_another_form_scores_one():
    r = _reward(CANCEL, _completion("cancel_order", order_id="#W1"))
    assert r.total == 1.0 and r.components["execution"] == 1.0


def test_the_right_tool_with_a_different_effect_keeps_name_and_format_credit():
    r = _reward(CANCEL, _completion("cancel_order", order_id="W2"))
    assert r.components == {
        "parse": 1.0,
        "decision": 1.0,
        "tool_exists": 1.0,
        "tool_name": 1.0,
        "schema": 1.0,
        "execution": 0.0,
    }
    assert r.total == pytest.approx(0.4) and "different effect" in r.reason


def test_a_wrong_tool_scores_below_the_right_tool_with_wrong_arguments():
    wrong_tool = _reward(CANCEL, _completion("get_order", order_id="W1"))
    assert wrong_tool.components["tool_name"] == 0.0 and wrong_tool.total == pytest.approx(0.2)
    assert wrong_tool.total < _reward(CANCEL, _completion("cancel_order", order_id="W2")).total


def test_a_hallucinated_tool_is_never_executed():
    EXECUTED.clear()
    r = _reward(CANCEL, _completion("cancel_everything", order_id="W1"))
    assert r.total == 0.0 and r.components["tool_exists"] == 0.0
    assert EXECUTED == []


def test_schema_failures_score_tool_exists_only_and_skip_execution():
    EXECUTED.clear()
    for bad in (
        _completion("cancel_order"),  # missing the required order_id
        _completion("cancel_order", order_id="W1", reason="x"),  # unknown argument
        _completion("cancel_order", order_id=1),  # wrong type
        _completion("cancel_order", order_id="W1", item_ids=[1]),  # wrong item type
    ):
        r = _reward(CANCEL, bad)
        assert r.components["schema"] == 0.0 and r.total == pytest.approx(0.3), bad
    assert EXECUTED == []


def test_unparsable_output_scores_zero():
    r = _reward(CANCEL, "<tool_call>{oops")
    assert r.total == 0.0 and r.components == {"parse": 0.0}


def test_reply_steps_score_the_decision_only():
    reply = Action(kind="reply", text="Your order W1 is cancelled.")
    assert (
        _reward(reply, "Anything else I can help with?" + IM_END).total == 1.0
    )  # text never compared
    called = _reward(reply, _completion("get_order", order_id="W1"))
    assert called.total == 0.0 and called.components["decision"] == 0.0


def test_a_reply_where_the_teacher_called_scores_zero():
    r = _reward(CANCEL, "Sure, I can help with that." + IM_END)
    assert r.total == 0.0 and r.components["decision"] == 0.0


def test_a_bool_is_not_a_number_and_nested_objects_are_checked():
    schema = {
        "type": "object",
        "properties": {
            "n": {"type": "integer"},
            "addr": {
                "type": "object",
                "properties": {"zip": {"type": "string"}},
                "required": ["zip"],
            },
        },
    }
    assert schema_ok({"n": 3, "addr": {"zip": "1"}}, schema)
    assert not schema_ok({"n": True}, schema)
    assert not schema_ok({"addr": {}}, schema)


def test_weights_must_name_exactly_the_call_components():
    assert check_weights(DEFAULT_WEIGHTS) == DEFAULT_WEIGHTS
    with pytest.raises(ValueError):
        check_weights({"execution": 1.0})
    with pytest.raises(ValueError):
        check_weights({"tool_exists": 0.0, "schema": 0.0, "execution": 0.0})


def test_the_reward_is_deterministic():
    samples = [
        (CANCEL, _completion("cancel_order", order_id="W1")),
        (CANCEL, _completion("cancel_order", order_id="W2")),
        (CANCEL, "no call here" + IM_END),
    ]
    assert_reward_deterministic(lambda s: _reward(*s), samples)

    flips = iter([0.0, 1.0, 0.0, 0.0, 0.0, 0.0])
    with pytest.raises(InvariantViolation):
        assert_reward_deterministic(lambda s: next(flips), samples)
