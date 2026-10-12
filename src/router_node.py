"""The router: a LangGraph node that keeps a turn on the student or escalates it
to the teacher (V1-18, plan §6 week 4).

SHARED COMPONENT (division-of-labor.md). Stage D's free-running sweep
(eval_free.py --escalate) and the served router (router_service.py) both decide
through `should_escalate`, so the curve in the README is the policy that ships.

The rule, given a step's confidence from src/logprobs.py:

    escalate  if step_confidence is None  or  step_confidence < threshold

  - step_confidence = min(p(call-vs-reply), tool-name confidence); DECIDED
    2026-09-25, see logprobs.py.
  - None escalates: an unparseable turn, or one with no first-position
    alternatives, is broken whatever its numbers say (logprobs.py, choice 6).
  - Strictly below: a threshold of 0.0 never escalates a measured step, and
    the plan's 0.5/0.7/0.85/0.95 compare against the same 0-1 geometric-mean
    probability the confidence is expressed in.
  - A prompt too long for the student's context also escalates
    (Reason.OVERFLOW): the student has no turn to offer, the teacher may.

The graph (build_router) is two nodes:

    START -> student -> (escalate and a teacher is configured) -> teacher -> END
                     \\-> END

`student` renders the conversation with src/prompts.serialize_state (the
serializer training and eval use), asks the student server for a completion
with log-probs, scores it and routes it. `teacher` asks the teacher model over
an OpenAI-compatible API with the same system prompt and catalog. Without a
teacher (the load test), an escalated turn is reported, not answered.

The rule is pure Python; langgraph and openai are imported inside the
functions that need them (tests/test_core_is_light.py).
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.logprobs import StepConfidence
from src.prompts import parse_completion, serialize_state


class Reason(StrEnum):
    CONFIDENT = "confident"  # the student's turn stands
    LOW_CONFIDENCE = "low_confidence"
    UNMEASURED = "unmeasured"  # no step confidence (unparsed, or no alternatives)
    OVERFLOW = "overflow"  # the prompt does not fit the student's context


@dataclass(frozen=True)
class Route:
    escalate: bool
    reason: Reason
    threshold: float
    step_confidence: float | None


def should_escalate(conf: StepConfidence | None, threshold: float) -> Route:
    """Route one turn. `conf` None means the student produced nothing to score
    (context overflow)."""
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"threshold {threshold} outside [0, 1]")
    if conf is None:
        return Route(True, Reason.OVERFLOW, threshold, None)
    s = conf.step_confidence
    if s is None:
        return Route(True, Reason.UNMEASURED, threshold, None)
    if s < threshold:
        return Route(True, Reason.LOW_CONFIDENCE, threshold, s)
    return Route(False, Reason.CONFIDENT, threshold, s)


# --- the served router ---------------------------------------------------------


class Student(Protocol):
    """serving.StudentClient, or a fake in tests."""

    def fits(self, prompt: str) -> bool: ...

    def score(self, prompt: str) -> tuple[str, StepConfidence]: ...


# messages (the serializer's dict shape) -> an action dict, as student_action returns
Teacher = Callable[[Sequence[Mapping[str, Any]]], dict[str, Any]]


def student_action(completion: str) -> dict[str, Any]:
    """A completion as the action a caller would execute: the tool call(s), or
    the reply text. An unparseable completion carries its error and no action."""
    parsed = parse_completion(completion)
    if parsed.action is None:
        return {"kind": "unparsed", "content": None, "tool_calls": [], "error": parsed.error}
    if parsed.action.kind == "reply":
        return {"kind": "reply", "content": parsed.action.text, "tool_calls": [], "error": None}
    calls = [{"name": c.name, "arguments": dict(c.arguments)} for c in parsed.action.calls]
    return {"kind": "tool_call", "content": None, "tool_calls": calls, "error": None}


class RouterState(TypedDict, total=False):
    messages: list[dict[str, Any]]
    prompt: str
    completion: str | None
    confidence: dict[str, Any] | None
    route: dict[str, Any]
    action: dict[str, Any] | None
    student_ms: float
    teacher_ms: float


def build_router(
    student: Student,
    teacher: Teacher | None,
    *,
    threshold: float,
    system: str,
    catalog: list[dict[str, Any]],
):
    """The compiled LangGraph app. Invoke with {"messages": [...]} or, to skip
    rendering (the load test replays rendered prompts), {"prompt": "..."}."""
    from langgraph.graph import END, START, StateGraph

    def student_node(state: RouterState) -> RouterState:
        started = time.perf_counter()
        prompt = state.get("prompt") or serialize_state(
            system=system, tools=catalog, messages=state.get("messages", [])
        )
        completion, conf = student.score(prompt) if student.fits(prompt) else (None, None)
        route = should_escalate(conf, threshold)
        return {
            "prompt": prompt,
            "completion": completion,
            "confidence": conf.as_dict() if conf is not None else None,
            "route": {**asdict(route), "reason": route.reason.value},
            # An escalated turn's action is the teacher's, or none without one.
            "action": None if route.escalate else student_action(completion or ""),
            "student_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    def teacher_node(state: RouterState) -> RouterState:
        assert teacher is not None
        messages = state.get("messages")
        if not messages:
            raise ValueError("the teacher answers a conversation, not a rendered prompt")
        started = time.perf_counter()
        action = teacher(messages)
        return {"action": action, "teacher_ms": round((time.perf_counter() - started) * 1000, 2)}

    def after_student(state: RouterState) -> str:
        escalate = (state.get("route") or {}).get("escalate", True)
        return "teacher" if escalate and teacher is not None else END

    graph = StateGraph(RouterState)
    graph.add_node("student", student_node)
    graph.add_node("teacher", teacher_node)
    graph.add_edge(START, "student")
    graph.add_conditional_edges("student", after_student, ["teacher", END])
    graph.add_edge("teacher", END)
    return graph.compile()


def route(app, request: Mapping[str, Any]) -> dict[str, Any]:
    """One routing decision: the response body router_service.py returns."""
    if "prompt" not in request and "messages" not in request:
        raise ValueError('a request needs "messages" (or a rendered "prompt")')
    if "messages" in request and not isinstance(request["messages"], list):
        raise ValueError('"messages" must be a list')
    started = time.perf_counter()
    state = app.invoke({k: request[k] for k in ("prompt", "messages") if k in request})
    return {
        "action": state.get("action"),
        "escalated": state["route"]["escalate"],
        "answered_by": (
            "student"
            if not state["route"]["escalate"]
            else ("teacher" if state.get("action") else "nobody")
        ),
        "route": state["route"],
        "student_completion": state.get("completion"),
        "confidence": state.get("confidence"),
        "student_ms": state.get("student_ms"),
        "teacher_ms": state.get("teacher_ms"),
        "total_ms": round((time.perf_counter() - started) * 1000, 2),
    }


# --- the teacher over an OpenAI-compatible API ----------------------------------


def openai_messages(system: str, messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The serializer's dict shape -> OpenAI chat messages. Tool results carry no
    id in that shape, so each one is matched, in order, to the oldest call still
    waiting for a result, which is how tau2 orders them."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    waiting: list[str] = []
    n = 0
    for m in messages:
        role = m["role"]
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                n += 1
                calls.append(
                    {
                        "id": f"call_{n}",
                        "type": "function",
                        "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
                    }
                )
            waiting += [c["id"] for c in calls]
            out.append({"role": "assistant", "content": m.get("content"), "tool_calls": calls})
        elif role == "tool":
            if not waiting:
                raise ValueError("a tool result with no call waiting for it")
            out.append({"role": "tool", "tool_call_id": waiting.pop(0), "content": m["content"]})
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return out


def openai_teacher(
    *,
    model: str,
    base_url: str,
    api_key: str,
    system: str,
    catalog: list[dict[str, Any]],
    max_tokens: int,
    temperature: float = 0.0,
) -> Teacher:
    """The teacher as a Teacher callable: the D7 baseline's model, temperature,
    system prompt and catalog, over an OpenAI-compatible endpoint (OpenRouter).
    Every call is a request against the account's daily quota."""
    from openai import OpenAI

    client = OpenAI(base_url=base_url, api_key=api_key)

    def ask(messages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        resp = client.chat.completions.create(
            model=model,
            messages=openai_messages(system, messages),  # type: ignore[arg-type]
            tools=catalog,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body={"provider": {"allow_fallbacks": False}},  # as harvest.teacher_llm_args
        )
        msg = resp.choices[0].message
        calls = [
            {"name": c.function.name, "arguments": json.loads(c.function.arguments or "{}")}
            for c in (msg.tool_calls or [])
            if c.type == "function"
        ]
        kind = "tool_call" if calls else "reply"
        return {"kind": kind, "content": msg.content, "tool_calls": calls, "error": None}

    return ask


def main(argv: list[str] | None = None) -> int:
    """Print the rule at each configured threshold, then route two fake turns
    through the compiled graph (`make smoke`; no server, no API)."""
    from src.cli import build_parser, resolve

    cfg = resolve(build_parser("Escalation rule").parse_args(argv))
    probe = [None, *[round(0.4 + 0.15 * i, 2) for i in range(5)]]
    for t in cfg.escalation.thresholds:
        cells = []
        for s in probe:
            conf = StepConfidence("reply", (), None, None, 0, None, False, s, s)
            cells.append(f"{s}->{'T' if should_escalate(conf, t).escalate else 's'}")
        print(f"[router_node] t={t:.2f}  " + "  ".join(cells))
    try:
        import langgraph  # noqa: F401
    except ImportError:  # CI's light requirements-dev.txt: the rule alone is the smoke
        print("[router_node] langgraph not installed; skipping the graph smoke")
        return 0

    class Fixed:
        def fits(self, prompt: str) -> bool:
            return True

        def score(self, prompt: str) -> tuple[str, StepConfidence]:
            s = 0.9 if "sure" in prompt else 0.1
            return "Hello.", StepConfidence("reply", (), None, None, 0, 1 - s, False, s, s)

    def teacher(messages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return {"kind": "reply", "content": "Teacher.", "tool_calls": [], "error": None}

    app = build_router(Fixed(), teacher, threshold=0.7, system="S", catalog=[])
    for text in ("I am sure", "No idea"):
        out = route(app, {"messages": [{"role": "user", "content": text, "tool_calls": None}]})
        print(
            f"[router_node] {text!r}: answered by {out['answered_by']} ({out['route']['reason']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
