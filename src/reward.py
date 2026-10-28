"""Stage G1: the step-level reward for GRPO (plan X7). Pure Python, no API.

For one train-split state (the teacher's context and action) and one sampled
completion:

  - 0 if the completion does not parse into an action.
  - Reply steps (the teacher replied): the decision type only, 1 for a reply and
    0 for a call. Reply text is never scored (plan X7, as in forced eval).
  - Tool-call steps (the teacher called): 0 for a reply. Otherwise a weighted sum
    of four components, normalised so a perfect call scores 1:
      tool_exists  every called tool is in the active catalog
      tool_name    the same tool(s) as the teacher, in order (added 2026-10-28,
                   Madhu: a right tool with wrong arguments should beat a wrong
                   tool); also 1 when the action is execution-equivalent
      schema       every call's arguments pass that tool's JSON schema
      execution    execution-equivalent to the teacher's action: the teacher's
                   earlier calls replayed on a fresh DB, then each action run on
                   its own copy; equal outputs and DB hash (make_pairs' rule)

So reply steps and call steps both top out at 1. Execution only runs when
tool_exists and schema pass: a hallucinated tool or a malformed call is never
executed. With the default weights: wrong tool, valid arguments 0.2; right tool,
invalid arguments 0.3; right tool, valid arguments, wrong effect 0.4.

Every component is returned, not just the total, so train_grpo can log them
separately; a policy that farms tool_exists + schema with useless read-only
calls shows up as those two rising while execution stays flat (plan X7).

The executor is passed in (plan X7's constraint): tests use a fake DB, real runs
use make_pairs.Tau2Runner, which imports tau2 lazily.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from src.make_pairs import Runner, execution_equivalent
from src.prompts import parse_completion
from src.trajectories import Action, ToolCall

CALL_COMPONENTS = ("tool_exists", "tool_name", "schema", "execution")
# Starting point, not a settled value (configs set grpo.reward_weights). Most of
# the weight on execution: the format checks are cheap to satisfy.
DEFAULT_WEIGHTS = {"tool_exists": 0.1, "tool_name": 0.2, "schema": 0.1, "execution": 0.6}

_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


@dataclass(frozen=True)
class Reward:
    total: float
    components: dict[str, float] = field(default_factory=dict)
    reason: str = ""  # why it is not 1, for sample inspection


def check_weights(weights: Mapping[str, float]) -> dict[str, float]:
    """Exactly the three call components, none negative, not all zero."""
    if set(weights) != set(CALL_COMPONENTS):
        raise ValueError(
            f"reward_weights must set exactly {CALL_COMPONENTS}, got {sorted(weights)}"
        )
    if any(w < 0 for w in weights.values()) or sum(weights.values()) <= 0:
        raise ValueError(f"reward_weights must be non-negative and not all zero: {dict(weights)}")
    return dict(weights)


def _type_ok(value: Any, schema: Mapping[str, Any]) -> bool:
    expected = schema.get("type")
    if expected is None:
        return True
    py = _JSON_TYPES.get(str(expected))
    if py is None:
        return True  # a type this catalog never uses; do not guess
    if isinstance(value, bool) and expected in ("integer", "number"):
        return False  # JSON booleans are not numbers, even though Python says so
    if not isinstance(value, py):
        return False
    if isinstance(value, list) and "items" in schema:
        return all(_type_ok(v, schema["items"]) for v in value)
    if isinstance(value, dict) and "properties" in schema:
        return schema_ok(value, schema)
    return True


def schema_ok(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> bool:
    """Required keys present, no unknown keys, each value of its declared type.

    Unknown keys fail: tau2's tools are Python functions, and an extra keyword
    argument is an error there (the 2026-10-26 pair review saw `order_id: ""`
    passed to modify_user_address).
    """
    props = schema.get("properties") or {}
    if any(key not in arguments for key in schema.get("required") or []):
        return False
    if any(key not in props for key in arguments):
        return False
    return all(_type_ok(arguments[key], props[key]) for key in arguments)


def step_reward(
    reference: Action,
    completion: str,
    context: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    run: Runner,
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
) -> Reward:
    """The reward for one completion at one state. See the module docstring."""
    w = check_weights(weights)
    parsed = parse_completion(completion)
    predicted = parsed.action
    if predicted is None:
        return Reward(0.0, {"parse": 0.0}, f"did not parse: {parsed.error}")
    if reference.kind == "reply":
        if predicted.kind == "reply":
            return Reward(1.0, {"parse": 1.0, "decision": 1.0})
        return Reward(
            0.0, {"parse": 1.0, "decision": 0.0}, "called a tool where the teacher replied"
        )
    if predicted.kind == "reply":
        return Reward(0.0, {"parse": 1.0, "decision": 0.0}, "replied where the teacher called")

    schemas = {t["function"]["name"]: t["function"].get("parameters") or {} for t in tools}
    c = {
        "parse": 1.0,
        "decision": 1.0,
        "tool_exists": 0.0,
        "tool_name": 0.0,
        "schema": 0.0,
        "execution": 0.0,
    }
    reason = ""
    if all(call.name in schemas for call in predicted.calls):
        c["tool_exists"] = 1.0
        same_names = [x.name for x in predicted.calls] == [x.name for x in reference.calls]
        c["tool_name"] = 1.0 if same_names else 0.0
        if all(schema_ok(call.arguments, schemas[call.name]) for call in predicted.calls):
            c["schema"] = 1.0
            if _same_calls(reference.calls, predicted.calls) or execution_equivalent(
                context, reference, predicted, run
            ):
                c["execution"] = c["tool_name"] = 1.0  # the same effect earns the name too
            else:
                reason = "a different effect from the teacher's call"
        else:
            reason = "arguments fail the tool's schema"
    else:
        reason = "a tool not in the catalog"
    total = sum(w[k] * c[k] for k in CALL_COMPONENTS) / sum(w.values())
    return Reward(round(total, 6), c, reason)


def _same_calls(a: Sequence[ToolCall], b: Sequence[ToolCall]) -> bool:
    """Identical calls need no execution to be equivalent."""
    return [(x.name, dict(x.arguments)) for x in a] == [(y.name, dict(y.arguments)) for y in b]
