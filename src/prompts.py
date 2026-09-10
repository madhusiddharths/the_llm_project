"""The shared prompt serializer. Imported by train AND eval. Never copied.

SHARED COMPONENT — division-of-labor.md puts this in the "Claude Code drafts,
you read every line before merge" table, and plan §10 ranks prompt-format drift
between train and eval a top-three risk (§6 Gate 2: the cause ~80% of the time).

Week 0 ships the *contract*: one module, one function, one hash. The body below
is deliberately minimal. Expand it in Week 1 with your review, and keep it the
only place a prompt is built.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from src.hashing import canonical_json, hash_obj

PROMPT_TEMPLATE = """You are a tool-routing assistant. Choose exactly one tool call that advances the user's request.

# Available tools
{tools}

# Conversation
{conversation}

Respond with a single JSON object: {{"name": <tool name>, "arguments": {{...}}}}"""


def _render_tool(tool: Mapping[str, object]) -> str:
    """One tool per line, canonical JSON so key order can never shift the prompt."""
    return canonical_json(tool)


def _render_message(message: Mapping[str, object]) -> str:
    role = str(message["role"]).strip()
    content = str(message.get("content", "")).strip()
    return f"{role}: {content}"


def serialize_state(
    *,
    tools: Sequence[Mapping[str, object]],
    messages: Sequence[Mapping[str, object]],
) -> str:
    """Render one (state) into the exact string the model sees.

    Keyword-only on purpose: a positional swap of tools and messages would still
    produce a plausible-looking prompt and quietly poison a whole run.
    """
    return PROMPT_TEMPLATE.format(
        tools="\n".join(_render_tool(t) for t in tools),
        conversation="\n".join(_render_message(m) for m in messages),
    )


# A fixed, tiny state used only to fingerprint the serializer. Never trained on.
_GOLDEN_STATE: dict[str, list[dict[str, object]]] = {
    "tools": [
        {
            "name": "get_order",
            "description": "Look up an order by id.",
            "parameters": {"order_id": {"type": "string"}},
        }
    ],
    "messages": [
        {"role": "user", "content": "where is order #42"},
    ],
}


def template_hash() -> str:
    """Fingerprint the serializer by hashing its OUTPUT on the golden state.

    Deliberately not a hash of PROMPT_TEMPLATE alone. Hashing the template only
    would let a change inside serialize_state() — reordered tool keys, a dropped
    strip(), a different join — sail past the train/eval equality check while
    changing every prompt the model actually sees.
    """
    return hash_obj({"rendered": serialize_state(**_GOLDEN_STATE)})
