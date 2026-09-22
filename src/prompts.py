"""The shared prompt serializer. Imported by train AND eval. Never copied.

SHARED COMPONENT: division-of-labor.md puts this in the "Claude Code drafts,
you read every line before merge" table, and plan §10 ranks prompt-format drift
between train and eval a top-three risk (§6 Gate 2: the cause ~80% of the time).

FORMAT (decided 2026-09-21, D1): Qwen2.5's own chat format, written out here in
pure Python rather than produced by the tokenizer's chat template.
  - ChatML turns: <|im_start|>role\\n...<|im_end|>\\n
  - A tool call is the assistant turn <tool_call>\\n{"name": ..., "arguments": ...}\\n</tool_call>
  - A tool result is a user turn wrapped in <tool_response> tags
  - A reply to the customer is the assistant turn's plain text
Why this and not a custom format: zero-shot Qwen already knows it, so the
Gate 2 zero-shot baseline is not handicapped by an unfamiliar format. Why pure
Python and not apply_chat_template: train and eval both feed the model this exact
string (vLLM and HF accept raw prompts), template_hash() pins all of it, and a
core module stays free of transformers (tests/test_core_is_light.py).

ONE DELIBERATE DEPARTURE from Qwen's template (D2): tools render as compact
signatures, not one JSON schema per line. That takes the 16 native tools from
~3.4k to ~2.2k tokens (estimated; results/data_card.md has measured prompt
lengths), and the saving grows with catalog size. Only JSON boilerplate is
dropped. Every description and
every argument description survives, because some carry the only statement of a
valid value (cancel_pending_order's reason must be 'no longer needed' or
'ordered by mistake'; order ids start with '#').

The action space mirrors tau2's agent contract: each turn is a reply OR a tool
call (src/trajectories.py, module note 2).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.hashing import hash_obj
from src.trajectories import Action, ToolCall, decision_steps, parse_action

IM_START = "<|im_start|>"
IM_END = "<|im_end|>"

# Qwen2.5's tool preamble, verbatim except that the <tools> block holds compact
# signatures (see module docstring). {tools} is the only substitution.
TOOLS_TEMPLATE = """# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{tools}
</tools>

For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
<tool_call>
{{"name": <function-name>, "arguments": <args-json-object>}}
</tool_call>"""

_TOOL_CALL = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


# --- tools -------------------------------------------------------------------


def _squash(text: object) -> str:
    """Collapse all whitespace. Schema descriptions wrap mid-sentence."""
    return " ".join(str(text or "").split())


def _type_of(prop: Mapping[str, Any]) -> str:
    if "anyOf" in prop:
        return " | ".join(_type_of(p) for p in prop["anyOf"])
    kind = prop.get("type")
    if isinstance(kind, list):
        return " | ".join(str(k) for k in kind)
    if kind == "array":
        return f"array[{_type_of(prop.get('items') or {})}]"
    if kind == "object" and prop.get("properties"):
        inner = ", ".join(f"{k}: {_type_of(v)}" for k, v in prop["properties"].items())
        return f"object{{{inner}}}"
    return str(kind or "any")


def render_tool(tool: Mapping[str, Any]) -> str:
    """One tool as a signature, its description, and one line per argument.

    Accepts the OpenAI shape ({"type": "function", "function": {...}}) or the
    bare function object. `?` marks an optional argument. Argument order is the
    schema's own order, fixed by the pinned catalog file.
    """
    fn = tool.get("function", tool)
    params = fn.get("parameters") or {}
    props: Mapping[str, Mapping[str, Any]] = params.get("properties") or {}
    required = set(params.get("required") or [])

    signature = ", ".join(
        f"{name}{'' if name in required else '?'}: {_type_of(prop)}" for name, prop in props.items()
    )
    lines = [f"{fn['name']}({signature})"]
    if description := _squash(fn.get("description")):
        lines.append(f"  {description}")
    for name, prop in props.items():
        note = _squash(prop.get("description"))
        if "enum" in prop:
            note = f"{note} One of: {', '.join(json.dumps(v) for v in prop['enum'])}.".strip()
        if note:
            lines.append(f"  - {name}: {note}")
    return "\n".join(lines)


def render_system(system_prompt: str, tools: Sequence[Mapping[str, Any]]) -> str:
    """The system turn's content: tau2's instructions and policy, then the tools."""
    rendered = "\n".join(render_tool(t) for t in tools)
    return f"{system_prompt.strip()}\n\n{TOOLS_TEMPLATE.format(tools=rendered)}"


# --- actions -----------------------------------------------------------------


def render_call(call: ToolCall) -> str:
    """Qwen's key order (name, then arguments); argument keys sorted, so one
    action has exactly one training target."""
    arguments = json.dumps(dict(call.arguments), ensure_ascii=False, sort_keys=True)
    return f'<tool_call>\n{{"name": {json.dumps(call.name, ensure_ascii=False)}, "arguments": {arguments}}}\n</tool_call>'


def render_action(action: Action) -> str:
    """An assistant turn's content, without the turn markers."""
    if action.kind == "reply":
        return action.text
    return "\n".join(render_call(c) for c in action.calls)


@dataclass(frozen=True)
class Parsed:
    """What a model completion turned out to be. `action` is None on failure."""

    action: Action | None
    error: str | None = None
    stray_text: bool = False  # text outside <tool_call> blocks, which tau2 would drop


def parse_completion(text: str) -> Parsed:
    """A generated assistant turn -> the action it expresses.

    Anything after the first <|im_end|> is discarded, which is where generation
    would have stopped. Any <tool_call> block makes the turn a tool call (tau2
    executes the call), so a malformed block is an error, never a reply.
    """
    text = text.split(IM_END, 1)[0]
    if "<tool_call>" not in text:
        reply = text.strip()
        if not reply:
            return Parsed(None, "empty completion")
        return Parsed(Action(kind="reply", text=reply))

    blocks = _TOOL_CALL.findall(text)
    if not blocks:
        return Parsed(None, "unclosed <tool_call>")
    calls = []
    for block in blocks:
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            return Parsed(None, "tool call is not valid JSON")
        if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
            return Parsed(None, "tool call has no string 'name'")
        arguments = obj.get("arguments", {})
        if isinstance(arguments, str):  # some models double-encode
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return Parsed(None, "tool call arguments are not valid JSON")
        if not isinstance(arguments, dict):
            return Parsed(None, "tool call arguments are not an object")
        calls.append(ToolCall(name=obj["name"], arguments=arguments))
    stray = bool(_TOOL_CALL.sub("", text).strip())
    return Parsed(Action(kind="tool_call", calls=tuple(calls)), stray_text=stray)


# --- turns -------------------------------------------------------------------


def _turn(role: str, content: str) -> str:
    return f"{IM_START}{role}\n{content}{IM_END}\n"


def _pieces(
    system: str, tools: Sequence[Mapping[str, Any]], messages: Sequence[Mapping[str, Any]]
) -> list[tuple[str, int | None]]:
    """The whole conversation as (text, message index) pieces.

    Assistant turns are split into header / content / close, so a decision's
    content can be marked trainable on its own. Consecutive tool results share
    one user turn, as in Qwen's template. serialize_state and serialize_episode
    both read this one list, which is what makes them agree by construction.
    """
    pieces: list[tuple[str, int | None]] = [(_turn("system", render_system(system, tools)), None)]
    i = 0
    while i < len(messages):
        message = messages[i]
        role = message.get("role")
        if role == "assistant":
            pieces.append((f"{IM_START}assistant\n", None))
            pieces.append((render_action(parse_action(message)), i))
            pieces.append((f"{IM_END}\n", None))
            i += 1
        elif role == "user":
            pieces.append((_turn("user", str(message.get("content") or "")), None))
            i += 1
        elif role == "tool":
            results = []
            while i < len(messages) and messages[i].get("role") == "tool":
                results.append(
                    f"<tool_response>\n{messages[i].get('content') or ''}\n</tool_response>"
                )
                i += 1
            pieces.append((_turn("user", "\n".join(results)), None))
        else:
            raise ValueError(f"message {i} has unsupported role {role!r}")
    return pieces


def serialize_state(
    *,
    system: str,
    tools: Sequence[Mapping[str, Any]],
    messages: Sequence[Mapping[str, Any]],
) -> str:
    """The exact prompt the model sees before its next turn.

    Keyword-only on purpose: a positional swap of tools and messages would still
    produce a plausible-looking prompt and quietly poison a whole run.
    """
    body = "".join(text for text, _ in _pieces(system, tools, messages))
    return f"{body}{IM_START}assistant\n"


@dataclass(frozen=True)
class Segment:
    text: str
    train: bool  # loss is computed on this text


def serialize_episode(
    *,
    system: str,
    tools: Sequence[Mapping[str, Any]],
    messages: Sequence[Mapping[str, Any]],
) -> list[Segment]:
    """A whole teacher episode as one training sequence.

    Loss falls on each teacher decision's content plus its <|im_end|> (so the
    model learns to stop), and nowhere else: not the greeting tau2 hardcodes,
    not user or tool turns. Because attention is causal, this trains on exactly
    the (serialize_state(prefix), decision) pairs that teacher-forced eval asks
    about, in one pass instead of one per decision.
    """
    decisions = {step.index for step in decision_steps(messages)}
    segments: list[Segment] = []
    pieces = _pieces(system, tools, messages)
    for k, (text, index) in enumerate(pieces):
        trainable = index is not None and index in decisions
        if trainable:
            segments.append(Segment(text + IM_END, True))
        elif k > 0 and pieces[k - 1][1] in decisions and text == f"{IM_END}\n":
            segments.append(Segment("\n", False))  # the <|im_end|> went with the decision
        else:
            segments.append(Segment(text, False))
    return segments


# --- fingerprint -------------------------------------------------------------

# A fixed, tiny episode used only to fingerprint the serializer. Never trained
# on. It exercises every branch that shapes a prompt: required, optional, enum
# and array arguments; a reply; a tool call; a tool result.
_GOLDEN: dict[str, Any] = {
    "system": "You are a customer service agent.",
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "cancel_order",
                "description": "Cancel a pending\norder.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "order_id": {"type": "string", "description": "Like '#W1'."},
                        "reason": {"type": "string", "enum": ["no longer needed", "mistake"]},
                        "item_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["order_id", "reason"],
                },
            },
        }
    ],
    "messages": [
        {"role": "assistant", "content": "Hi! How can I help you today?"},
        {"role": "user", "content": "cancel #W1"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"name": "cancel_order", "arguments": {"reason": "mistake", "order_id": "#W1"}}
            ],
        },
        {"role": "tool", "content": '{"status": "cancelled"}'},
        {"role": "assistant", "content": "Done — it is cancelled."},
        {"role": "user", "content": "###STOP###"},
    ],
}


def template_hash() -> str:
    """Fingerprint the serializer by hashing its OUTPUT on the golden episode.

    Deliberately not a hash of the template strings alone. That would let a
    change inside the rendering functions (reordered keys, a dropped strip(), a
    different join) sail past the train/eval equality check while changing every
    prompt the model actually sees. Both the eval prompt and the training
    segments are covered, since each is a path to the model.
    """
    return hash_obj(
        {
            "state": serialize_state(**_GOLDEN),
            "episode": [[s.text, s.train] for s in serialize_episode(**_GOLDEN)],
        }
    )
