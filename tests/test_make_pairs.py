"""E2 pair building, pure half: a fake DB stands in for tau2's retail env."""

from __future__ import annotations

import json

from src.make_pairs import State, as_target, build_pairs, execution_equivalent, judge, prior_calls
from src.prompts import IM_END
from src.trajectories import Action, ToolCall

CATALOG = ["get_order", "cancel_order", "find_user"]
ORDERS = {"W1": "pending", "W2": "delivered"}


def fake_run(prefix, calls):
    """A tiny deterministic environment: ids tolerate a leading '#', like a lenient
    tool would, so '#W1' and 'W1' are the same call in effect but not in form."""
    db = dict(ORDERS)

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
    outs = [call(c) for c in calls]
    return outs, json.dumps(db, sort_keys=True)


def _call(name, **args):
    return Action(kind="tool_call", calls=(ToolCall(name=name, arguments=args),))


def _completion(name, **args):
    return (
        "<tool_call>\n" + json.dumps({"name": name, "arguments": args}) + "\n</tool_call>" + IM_END
    )


CONTEXT = (
    {"role": "assistant", "content": "Hi! How can I help you today?"},
    {"role": "user", "content": "cancel W1"},
)


def test_prior_calls_are_the_teachers_earlier_tool_calls_in_order():
    ctx = (
        *CONTEXT,
        {
            "role": "assistant",
            "tool_calls": [{"name": "get_order", "arguments": {"order_id": "W1"}}],
        },
        {"role": "tool", "content": "W1:pending"},
    )
    assert [c.name for c in prior_calls(ctx)] == ["get_order"]


def test_same_effect_in_another_form_is_equivalent_not_a_failure():
    ref = _call("get_order", order_id="W1")
    assert execution_equivalent(CONTEXT, ref, _call("get_order", order_id="#W1"), fake_run)
    assert not execution_equivalent(CONTEXT, ref, _call("get_order", order_id="W2"), fake_run)
    v = judge(ref, _completion("get_order", order_id="#W1"), CATALOG, CONTEXT, fake_run)
    assert v.outcome == "equivalent"


def test_a_write_with_a_different_effect_is_a_pair():
    ref = _call("cancel_order", order_id="W1")
    v = judge(ref, _completion("cancel_order", order_id="W2"), CATALOG, CONTEXT, fake_run)
    assert v.outcome == "pair" and v.bucket == "right_tool_wrong_args"


def test_reply_text_is_never_compared():
    reply = Action(kind="reply", text="Done, W1 is cancelled.")
    assert (
        judge(reply, "Something else entirely." + IM_END, CATALOG, CONTEXT, None).outcome == "agree"
    )
    v = judge(reply, _completion("get_order", order_id="W1"), CATALOG, CONTEXT, fake_run)
    assert v.outcome == "pair" and v.bucket == "call_instead_of_reply"


def test_malformed_and_premature_outputs_are_pairs_with_their_bucket():
    ref = _call("get_order", order_id="W1")
    assert judge(ref, "<tool_call>{oops", CATALOG, CONTEXT, fake_run).bucket == "malformed_output"
    assert (
        judge(ref, "Anything else?", CATALOG, CONTEXT, fake_run).bucket == "premature_termination"
    )
    assert judge(ref, _completion("nope", order_id="W1"), CATALOG, CONTEXT, fake_run).bucket == (
        "hallucinated_tool"
    )


def test_build_pairs_counts_dedupes_and_renders_both_sides():
    ref = _call("cancel_order", order_id="W1")
    state = State("5:0:2", "5", 0, 2, "PROMPT", CONTEXT, ref)
    wrong = _completion("cancel_order", order_id="W2")
    completions = [
        {"step_id": "5:0:2", "completion": _completion("cancel_order", order_id="W1")},  # agree
        {
            "step_id": "5:0:2",
            "sample": 1,
            "completion": _completion("cancel_order", order_id="#W1"),
        },
        {"step_id": "5:0:2", "sample": 2, "completion": wrong},
        {"step_id": "5:0:2", "sample": 3, "completion": wrong + " trailing text after stop"},
    ]
    pairs, stats = build_pairs([state], completions, CATALOG, fake_run)
    assert stats["outcomes"] == {"agree": 1, "equivalent": 1, "pair": 2, "duplicate": 1}
    assert stats["n_pairs"] == 1 and stats["pairs_per_bucket"] == {"right_tool_wrong_args": 1}
    assert (
        stats["call_disagreements"] == 3
        and stats["equivalent_share_of_call_disagreements"] == 0.3333
    )
    (p,) = pairs
    assert (
        p["prompt"] == "PROMPT"
        and p["chosen"].endswith(IM_END)
        and p["rejected"] == as_target(wrong)
    )
    assert '"order_id": "W1"' in p["chosen"] and '"W2"' in p["rejected"]
