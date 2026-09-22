"""The serializer: what the model sees, and what it is trained to say.

The properties that matter most are the ones that would fail silently:
  - training targets line up exactly with the eval prompt at every decision,
  - rendering an action and parsing it back is the identity,
  - compact tool rendering drops boilerplate but never a description.
"""

from __future__ import annotations

import pytest

from src.prompts import (
    _GOLDEN,
    IM_END,
    parse_completion,
    render_action,
    render_tool,
    serialize_episode,
    serialize_state,
)
from src.trajectories import Action, ToolCall, decision_steps


def _golden():
    return dict(_GOLDEN)


def test_prompt_ends_with_an_open_assistant_turn():
    assert serialize_state(**_golden()).endswith("<|im_start|>assistant\n")


def test_every_training_target_follows_exactly_its_eval_prompt():
    """The core train/eval contract. Causal attention makes one sequence
    equivalent to one (prompt, target) pair per decision only if this holds."""
    g = _golden()
    segments = serialize_episode(**g)
    steps = decision_steps(g["messages"])
    trained = [k for k, s in enumerate(segments) if s.train]
    assert len(trained) == len(steps)
    for k, step in zip(trained, steps, strict=True):
        prefix = "".join(s.text for s in segments[:k])
        assert prefix == serialize_state(
            system=g["system"], tools=g["tools"], messages=step.context
        )
        assert segments[k].text == render_action(step.action) + IM_END


def test_the_episode_text_is_the_full_conversation_with_nothing_added():
    g = _golden()
    text = "".join(s.text for s in serialize_episode(**g))
    assert text + "<|im_start|>assistant\n" == serialize_state(**g)


def test_the_hardcoded_greeting_is_never_a_training_target():
    trained = [s.text for s in serialize_episode(**_golden()) if s.train]
    assert not any("How can I help" in t for t in trained)


def test_tool_results_render_as_a_user_turn_in_tool_response_tags():
    prompt = serialize_state(**_golden())
    assert (
        '<|im_start|>user\n<tool_response>\n{"status": "cancelled"}\n</tool_response><|im_end|>'
        in prompt
    )


def test_consecutive_tool_results_share_one_user_turn():
    g = _golden()
    g["messages"] = [
        *g["messages"][:3],
        {"role": "tool", "content": "a"},
        {"role": "tool", "content": "b"},
    ]
    prompt = serialize_state(**g)
    assert "<tool_response>\na\n</tool_response>\n<tool_response>\nb\n</tool_response>" in prompt


@pytest.mark.parametrize(
    "action",
    [
        Action(kind="reply", text="Could you confirm your zip code?"),
        Action(kind="tool_call", calls=(ToolCall("get_order_details", {"order_id": "#W1"}),)),
        Action(
            kind="tool_call",
            calls=(ToolCall("exchange", {"item_ids": ["1", "2"], "note": "café — ok"}),),
        ),
        Action(kind="tool_call", calls=(ToolCall("a", {}), ToolCall("b", {"x": 1}))),
    ],
)
def test_render_then_parse_is_the_identity(action):
    parsed = parse_completion(render_action(action) + IM_END + "trailing junk")
    assert parsed.error is None
    assert parsed.action == action
    assert not parsed.stray_text


def test_tool_call_json_has_sorted_argument_keys_and_name_first():
    text = render_action(Action(kind="tool_call", calls=(ToolCall("f", {"b": 1, "a": 2}),)))
    assert text == '<tool_call>\n{"name": "f", "arguments": {"a": 2, "b": 1}}\n</tool_call>'


@pytest.mark.parametrize(
    ("completion", "error"),
    [
        ("", "empty completion"),
        ("   " + IM_END, "empty completion"),
        ('<tool_call>\n{"name": "f"', "unclosed <tool_call>"),
        ("<tool_call>\nnot json\n</tool_call>", "not valid JSON"),
        ('<tool_call>\n{"arguments": {}}\n</tool_call>', "no string 'name'"),
        ('<tool_call>\n{"name": "f", "arguments": [1]}\n</tool_call>', "not an object"),
    ],
)
def test_malformed_completions_are_errors_not_replies(completion, error):
    parsed = parse_completion(completion)
    assert parsed.action is None
    assert error in parsed.error


def test_double_encoded_arguments_are_accepted():
    parsed = parse_completion(
        '<tool_call>\n{"name": "f", "arguments": "{\\"x\\": 1}"}\n</tool_call>'
    )
    assert parsed.action.calls[0].arguments == {"x": 1}


def test_text_around_a_tool_call_is_flagged_and_the_call_wins():
    parsed = parse_completion('Sure!\n<tool_call>\n{"name": "f", "arguments": {}}\n</tool_call>')
    assert parsed.action.kind == "tool_call"
    assert parsed.stray_text


def test_compact_tool_keeps_every_description_and_marks_optionals():
    tool = _GOLDEN["tools"][0]
    text = render_tool(tool)
    assert text.splitlines()[0] == (
        "cancel_order(order_id: string, reason: string, item_ids?: array[string])"
    )
    assert "Cancel a pending order." in text  # wrapped description squashed onto one line
    assert "- order_id: Like '#W1'." in text
    assert '- reason: One of: "no longer needed", "mistake".' in text


def test_bare_function_objects_render_like_openai_wrapped_ones():
    tool = _GOLDEN["tools"][0]
    assert render_tool(tool["function"]) == render_tool(tool)


def test_an_unknown_role_is_an_error():
    g = _golden()
    g["messages"] = [*g["messages"][:2], {"role": "system", "content": "x"}]
    with pytest.raises(ValueError, match="unsupported role"):
        serialize_state(**g)
