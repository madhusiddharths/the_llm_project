"""The escalation rule: which turns go to the teacher."""

from __future__ import annotations

import pytest

from src.logprobs import StepConfidence
from src.router_node import Reason, should_escalate


def conf(step: float | None, kind: str = "tool_call") -> StepConfidence:
    return StepConfidence(kind, (), step, None, 1, step, False, step, step)


def test_below_the_threshold_escalates_and_at_it_does_not():
    assert should_escalate(conf(0.69), 0.7).escalate
    assert should_escalate(conf(0.69), 0.7).reason is Reason.LOW_CONFIDENCE
    route = should_escalate(conf(0.7), 0.7)
    assert not route.escalate and route.reason is Reason.CONFIDENT
    assert route.step_confidence == 0.7 and route.threshold == 0.7


def test_no_measurable_confidence_always_escalates():
    """An unparsed turn or one without first-position alternatives (logprobs.py, 6)."""
    unparsed = StepConfidence("unparsed", (), None, None, 0, None, False, None, None)
    for t in (0.0, 0.5, 0.95):
        route = should_escalate(unparsed, t)
        assert route.escalate and route.reason is Reason.UNMEASURED


def test_a_context_overflow_escalates():
    route = should_escalate(None, 0.0)
    assert route.escalate and route.reason is Reason.OVERFLOW


def test_threshold_zero_never_escalates_a_measured_step_and_one_escalates_almost_all():
    assert not should_escalate(conf(0.0), 0.0).escalate
    assert should_escalate(conf(0.999), 1.0).escalate
    assert not should_escalate(conf(1.0), 1.0).escalate


def test_replies_are_routed_by_the_same_number():
    assert should_escalate(conf(0.4, kind="reply"), 0.5).escalate


@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_a_threshold_outside_zero_one_is_refused(bad):
    with pytest.raises(ValueError):
        should_escalate(conf(0.5), bad)


# --- the LangGraph router -------------------------------------------------------


class FakeStudent:
    """Confidence from the last user message: 'sure' -> 0.9, anything else 0.2."""

    def __init__(self, completion="Hello.", fits=True):
        self.completion, self._fits, self.prompts = completion, fits, []

    def fits(self, prompt):
        return self._fits

    def score(self, prompt):
        self.prompts.append(prompt)
        s = 0.9 if "sure" in prompt else 0.2
        kind = "tool_call" if self.completion.startswith("<tool_call>") else "reply"
        return self.completion, StepConfidence(kind, (), s, None, 1, s, False, s, s)


def _teacher(calls):
    def ask(messages):
        calls.append(messages)
        return {"kind": "reply", "content": "Teacher.", "tool_calls": [], "error": None}

    return ask


def _app(student, teacher, threshold=0.7):
    pytest.importorskip("langgraph")
    from src.router_node import build_router

    return build_router(student, teacher, threshold=threshold, system="SYS", catalog=[])


def _msgs(text):
    return [{"role": "user", "content": text, "tool_calls": None}]


def test_a_confident_turn_is_the_students_and_the_teacher_is_not_asked():
    from src.prompts import serialize_state
    from src.router_node import route

    calls, student = (
        [],
        FakeStudent('<tool_call>\n{"name": "f", "arguments": {"x": 1}}\n</tool_call>'),
    )
    out = route(_app(student, _teacher(calls)), {"messages": _msgs("I am sure")})
    assert out["answered_by"] == "student" and not out["escalated"] and calls == []
    assert out["action"] == {
        "kind": "tool_call",
        "content": None,
        "tool_calls": [{"name": "f", "arguments": {"x": 1}}],
        "error": None,
    }
    # The student saw the shared serializer's rendering, nothing else.
    assert student.prompts == [serialize_state(system="SYS", tools=[], messages=_msgs("I am sure"))]


def test_an_unsure_turn_goes_to_the_teacher_with_the_conversation():
    from src.router_node import route

    calls = []
    out = route(_app(FakeStudent(), _teacher(calls)), {"messages": _msgs("hmm")})
    assert out["answered_by"] == "teacher" and out["route"]["reason"] == "low_confidence"
    assert out["action"]["content"] == "Teacher." and calls == [_msgs("hmm")]
    assert out["student_completion"] == "Hello." and out["confidence"]["step_confidence"] == 0.2


def test_without_a_teacher_an_escalation_is_reported_not_answered():
    from src.router_node import route

    out = route(_app(FakeStudent(), None), {"prompt": "rendered, hmm"})
    assert out["escalated"] and out["answered_by"] == "nobody" and out["action"] is None


def test_an_overflowing_prompt_escalates_without_asking_the_student():
    from src.router_node import route

    student = FakeStudent(fits=False)
    out = route(_app(student, _teacher([])), {"messages": _msgs("I am sure")})
    assert out["route"]["reason"] == "overflow" and out["answered_by"] == "teacher"
    assert student.prompts == []


def test_a_request_without_messages_or_prompt_is_refused():
    from src.router_node import route

    with pytest.raises(ValueError, match="messages"):
        route(_app(FakeStudent(), None), {"text": "hi"})


def test_an_unparseable_completion_is_never_executed():
    from src.router_node import student_action

    out = student_action("<tool_call>\n{not json\n</tool_call>")
    assert out["kind"] == "unparsed" and out["tool_calls"] == [] and out["error"]


def test_openai_messages_pair_each_tool_result_with_its_call_in_order():
    from src.router_node import openai_messages

    msgs = [
        {"role": "user", "content": "hi", "tool_calls": None},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"name": "a", "arguments": {"x": 1}}, {"name": "b", "arguments": {}}],
        },
        {"role": "tool", "content": "ra", "tool_calls": None},
        {"role": "tool", "content": "rb", "tool_calls": None},
        {"role": "assistant", "content": "done", "tool_calls": None},
    ]
    out = openai_messages("SYS", msgs)
    assert out[0] == {"role": "system", "content": "SYS"}
    ids = [c["id"] for c in out[2]["tool_calls"]]
    assert out[2]["tool_calls"][0]["function"] == {"name": "a", "arguments": '{"x": 1}'}
    assert [out[3]["tool_call_id"], out[4]["tool_call_id"]] == ids
    assert out[5] == {"role": "assistant", "content": "done"}


def test_a_tool_result_without_a_call_is_refused():
    from src.router_node import openai_messages

    with pytest.raises(ValueError, match="no call"):
        openai_messages("S", [{"role": "tool", "content": "r", "tool_calls": None}])
