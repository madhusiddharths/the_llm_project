"""Step scoring against plan §3. Each case is a way a student can be wrong."""

from __future__ import annotations

import pytest

from src.metrics import calls_equal, normalize, score_step, summarize
from src.prompts import Parsed, parse_completion
from src.trajectories import Action, ToolCall

CATALOG = ["get_order_details", "cancel_pending_order"]


def call(name, **args):
    return Action(kind="tool_call", calls=(ToolCall(name, args),))


REF = call("cancel_pending_order", order_id="#W1", reason="ordered by mistake")


def test_normalized_match_ignores_case_whitespace_and_key_order():
    pred = call("cancel_pending_order", reason="  Ordered by Mistake ", order_id="#w1")
    assert calls_equal(REF, pred, strict=False)
    assert not calls_equal(REF, pred, strict=True)


def test_no_type_coercion_between_string_and_number():
    assert not calls_equal(call("f", x="1"), call("f", x=1), strict=False)


def test_numeric_tolerance_is_zero():
    assert calls_equal(call("f", x=3), call("f", x=3.0), strict=False)
    assert not calls_equal(call("f", x=3), call("f", x=3.0001), strict=False)


def test_list_order_matters():
    """exchange pairs item_ids[i] with new_item_ids[i]."""
    assert not calls_equal(call("f", ids=["1", "2"]), call("f", ids=["2", "1"]), strict=False)


def test_normalize_recurses():
    assert normalize({"A": [" X ", {"B": "Y"}]}) == {"a": ["x", {"b": "y"}]}


def _score(completion, reference=REF):
    return score_step(reference, parse_completion(completion), CATALOG)


def test_exact_agreement():
    s = _score(
        '<tool_call>\n{"name": "cancel_pending_order", "arguments": {"order_id": "#W1", "reason": "ordered by mistake"}}\n</tool_call>'
    )
    assert s.kind_match and s.name_match and s.normalized_match and s.strict_match


def test_right_tool_wrong_arguments():
    s = _score(
        '<tool_call>\n{"name": "cancel_pending_order", "arguments": {"order_id": "#W2", "reason": "ordered by mistake"}}\n</tool_call>'
    )
    assert s.name_match and not s.normalized_match


def test_hallucinated_tool_is_flagged():
    s = _score('<tool_call>\n{"name": "cancel_order", "arguments": {}}\n</tool_call>')
    assert s.hallucinated_tool and not s.name_match


def test_premature_reply_instead_of_a_call():
    s = _score("I have cancelled it.")
    assert s.predicted_kind == "reply" and not s.kind_match and not s.normalized_match


def test_a_parse_failure_scores_as_wrong_on_everything():
    s = score_step(REF, Parsed(None, "tool call is not valid JSON"), CATALOG)
    assert s.predicted_kind is None and s.parse_error
    assert not (s.kind_match or s.name_match or s.normalized_match)


def test_reply_steps_are_scored_on_decision_type_only():
    ref = Action(kind="reply", text="What is your email?")
    assert _score("Could I have your email address?", ref).kind_match
    wrong = _score('<tool_call>\n{"name": "get_order_details", "arguments": {}}\n</tool_call>', ref)
    assert not wrong.kind_match


def test_summary_computes_call_metrics_over_call_steps_only():
    rows = [
        _score(
            '<tool_call>\n{"name": "cancel_pending_order", "arguments": {"order_id": "#W1", "reason": "ordered by mistake"}}\n</tool_call>'
        ),
        _score("done"),  # call step answered with a reply
        _score("hello", Action(kind="reply", text="hi")),  # reply step, right kind
    ]
    s = summarize(rows)
    assert (s["n_tool_call_steps"], s["n_reply_steps"]) == (2, 1)
    assert s["step_agreement"] == 0.5
    assert s["decision_type_accuracy"] == pytest.approx(2 / 3, abs=1e-4)
    assert s["reply_step_decision_accuracy"] == 1.0
