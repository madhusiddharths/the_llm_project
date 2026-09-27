"""Escalation preview: the arithmetic of the sweep, on hand-built steps."""

from __future__ import annotations

import pytest

from src.escalation_sim import auroc, signal, step_right, sweep
from src.logprobs import StepConfidence


def conf(kind, name=None, step=None):
    return StepConfidence(kind, (), name, None, 0, None, False, None, step)


def row(kind, right):
    if kind == "tool_call":
        return {"reference_kind": "tool_call", "normalized_match": right, "kind_match": True}
    return {"reference_kind": "reply", "normalized_match": False, "kind_match": right}


def test_a_reply_step_is_right_when_the_decision_is():
    assert step_right(row("reply", True)) and not step_right(row("reply", False))
    assert step_right(row("tool_call", True)) and not step_right(row("tool_call", False))


def test_name_only_never_escalates_a_reply_but_step_can():
    c = conf("reply", step=0.3)
    assert signal(c, "name_only") == 1.0
    assert signal(c, "step") == 0.3
    assert signal(conf("unparsed"), "step") is None  # always escalates


def test_auroc_is_one_when_every_wrong_step_is_less_confident():
    assert auroc([0.1, 0.2, 0.9, 0.95], [True, True, False, False]) == 1.0
    assert auroc([0.5, 0.5], [True, False]) == 0.5
    assert auroc([0.5], [True]) is None


def test_sweep_counts_escalations_and_catches():
    confs = {
        "a": conf("tool_call", name=0.95, step=0.95),  # right, confident
        "b": conf("tool_call", name=0.40, step=0.40),  # wrong, unsure: caught at 0.5
        "c": conf("reply", step=0.60),  # wrong reply: only "step" can catch it
        "d": conf("unparsed"),  # wrong, always escalated
    }
    rows = {
        "a": row("tool_call", True),
        "b": row("tool_call", False),
        "c": row("reply", False),
        "d": row("tool_call", False),
    }
    name = sweep(confs, rows, [0.5, 0.7], "name_only")
    step = sweep(confs, rows, [0.5, 0.7], "step")
    assert name["accuracy_before"] == 0.25
    # t=0.7, name-only: b and d escalate; the wrong reply c slips through.
    p = name["points"][1]
    assert p["escalation_rate"] == 0.5 and p["accuracy_after"] == 0.75
    assert p["mistakes_caught"] == pytest.approx(2 / 3, abs=1e-3)
    # t=0.7, step: c escalates too, and every mistake is caught.
    p = step["points"][1]
    assert p["escalation_rate"] == 0.75 and p["accuracy_after"] == 1.0
    assert p["wasted_escalations"] == 0.0
    assert step["unmeasured_steps"] == 1
