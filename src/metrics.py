"""Per-step scoring: does a predicted action agree with the reference action?

SHARED COMPONENT ("argument normalization", division-of-labor.md): it must
implement the rules plan §3 fixed, not rules invented here. Those rules:

    normalized: lowercase strings, strip whitespace, numeric tolerance 0,
                ignore key order
    strict:     exact match, reported alongside so the choice is visible

Choices §3 does not spell out, made here and open to review:
  - No type coercion. "1008292230" and 1008292230 do not match; the schema says
    string, and a model that emits the wrong type has made an argument error.
  - List order matters. exchange_delivered_order_items pairs item_ids[i] with
    new_item_ids[i], so a reordered list is a different request.
  - Parallel calls are compared as an ordered list (the teacher never made one).
  - Metrics by step kind (D3, decided 2026-09-21): step agreement and tool-name
    accuracy are over TOOL-CALL steps only. Reply steps are scored for decision
    type (reply vs call) and nothing else. Reply text is never scored.

Pure Python: a core module, reused by eval_forced, eval_free and taxonomy.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from src.prompts import Parsed
from src.trajectories import Action


def normalize(value: Any) -> Any:
    """§3's normalized form. Recurses through lists and objects."""
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return value  # tolerance 0: 3 == 3.0 as Python compares, nothing looser
    if isinstance(value, list | tuple):
        return [normalize(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k).strip().lower(): normalize(v) for k, v in value.items()}
    return value


def _calls(action: Action) -> list[tuple[str, Any]]:
    return [(c.name, dict(c.arguments)) for c in action.calls]


def calls_equal(a: Action, b: Action, *, strict: bool) -> bool:
    if a.kind != "tool_call" or b.kind != "tool_call" or len(a.calls) != len(b.calls):
        return False
    if strict:
        return _calls(a) == _calls(b)
    return [(n.strip().lower(), normalize(args)) for n, args in _calls(a)] == [
        (n.strip().lower(), normalize(args)) for n, args in _calls(b)
    ]


@dataclass(frozen=True)
class StepScore:
    reference_kind: str  # "tool_call" | "reply"
    predicted_kind: str | None  # None when the completion did not parse
    parse_error: str | None
    kind_match: bool  # reply-vs-call decision agrees
    name_match: bool  # tool-call steps: same tool name(s)
    normalized_match: bool  # tool-call steps: step agreement (§3 normalized)
    strict_match: bool  # tool-call steps: step agreement (strict)
    hallucinated_tool: bool  # predicted a tool name not in the active catalog

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def score_step(reference: Action, parsed: Parsed, catalog_names: Iterable[str]) -> StepScore:
    predicted = parsed.action
    names = set(catalog_names)
    kind = predicted.kind if predicted else None
    is_call = reference.kind == "tool_call"
    both_calls = is_call and kind == "tool_call"
    return StepScore(
        reference_kind=reference.kind,
        predicted_kind=kind,
        parse_error=parsed.error,
        kind_match=kind == reference.kind,
        name_match=both_calls
        and [c.name for c in predicted.calls] == [c.name for c in reference.calls],
        normalized_match=both_calls and calls_equal(reference, predicted, strict=False),
        strict_match=both_calls and calls_equal(reference, predicted, strict=True),
        hallucinated_tool=kind == "tool_call" and any(c.name not in names for c in predicted.calls),
    )


def summarize(scores: Iterable[StepScore | Mapping[str, Any]]) -> dict[str, Any]:
    """Headline per-step numbers. Tool-call metrics over tool-call steps only (D3)."""
    rows = [s.as_dict() if isinstance(s, StepScore) else dict(s) for s in scores]
    calls = [r for r in rows if r["reference_kind"] == "tool_call"]
    replies = [r for r in rows if r["reference_kind"] == "reply"]

    def rate(xs: list[dict[str, Any]], key: str) -> float | None:
        return round(sum(bool(x[key]) for x in xs) / len(xs), 4) if xs else None

    return {
        "n_steps": len(rows),
        "n_tool_call_steps": len(calls),
        "n_reply_steps": len(replies),
        "step_agreement": rate(calls, "normalized_match"),
        "step_agreement_strict": rate(calls, "strict_match"),
        "tool_name_accuracy": rate(calls, "name_match"),
        "decision_type_accuracy": rate(rows, "kind_match"),
        "reply_step_decision_accuracy": rate(replies, "kind_match"),
        "parse_error_rate": round(sum(r["parse_error"] is not None for r in rows) / len(rows), 4)
        if rows
        else None,
        "hallucinated_tool_rate": rate(rows, "hallucinated_tool"),
    }
