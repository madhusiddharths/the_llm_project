"""Step confidence from per-token log-probs (V1-18, plan §6 week 4).

SHARED COMPONENT (division-of-labor.md): a bug here gives a plausible but wrong
escalation curve, and nothing downstream would notice. Read it line by line.

The escalation check sends a step to the teacher when the student is unsure of
it. A step is two choices, and each gets its own number:

    decision_confidence  how sure it was about calling a tool vs replying
    name_confidence      how sure it was of the tool name (tool calls only)
    step_confidence      min of the two; this is what the threshold is applied to

DECIDED 2026-09-25 (Madhu): name + decision, not the plan's name-only signal.
A name-only check has nothing to measure on a reply, so it can never stop a
reply that should have been a tool call, nor weigh a call that should have been
a reply (200 such steps for the 1.5B SFT at catalog 80). One min() keeps a
single threshold, so Stage D's four thresholds sweep one number as planned.

    name_confidence = exp(mean log p(token) over the tokens of the name)

Choices, each one a place this could silently go wrong:

1. ONLY THE NAME. Not the whole completion (the argument tokens and the JSON
   braces are near-certain and would drag every step towards 1.0), and not the
   `{"name": "` prefix, which is boilerplate the model always gets right.
2. MEAN, NOT SUM. A summed log-prob falls with name length, so
   `find_user_id_by_name_zip` (7 tokens) would look less sure than `calculate`
   (1 token) at equal per-token certainty. The mean is length-neutral, and
   exp(mean) is the geometric-mean probability, on the same 0-1 scale as the
   thresholds in base.yaml (0.5/0.7/0.85/0.95).
3. TOKENS ARE MATCHED BY CHARACTER OFFSET. A token belongs to the name if its
   characters overlap the name's characters in the decoded completion. In
   Qwen2.5's tokenizer the name tokens stand alone (`get`, `_order`,
   `_details`; the quote is its own ` "` token), but a compact rendering can
   fuse a quote into a boundary token (`":"`, `","`). Overlap keeps such a
   token only if it carries name characters; it can never pull in a token that
   is all punctuation.
4. SEVERAL CALLS IN ONE TURN: name confidence is the MINIMUM over the calls.
   The turn goes to the teacher if any one name is doubtful, since tau2
   executes all of them.
5. THE DECISION IS p(<tool_call>) AT THE FIRST POSITION, not the probability of
   whatever first token was emitted. On Qwen2.5 `<tool_call>` is one token, so
   p_call is exactly the model's belief that this turn is a tool call. A reply's
   own first token is the wrong measure: "I", "Your" and "Thanks" split the
   reply mass between them, so a certain reply would look unsure. So:
       tool call -> decision_confidence = p_call
       reply     -> decision_confidence = 1 - p_call
   p_call is read from the top-k alternatives recorded at position 0. If
   <tool_call> is not among them, p_call is at most the k-th probability, and
   that bound is used: 1 - bound is then the LOWEST the reply's confidence can
   be, so the error can only err towards escalating. (A tool call whose first
   token was not <tool_call>, i.e. text before the call, gets the bound as its
   decision confidence. With k=20 the bound is at most 0.05, so it escalates.)
6. NOTHING MEASURABLE, NO CONFIDENCE. A completion that does not parse gets
   step_confidence=None, and so does a step recorded without first-position
   alternatives. The escalation check treats None as "escalate" (router_node.py):
   an unparseable turn is broken whatever its numbers say.
7. THE TEXT IS RE-DECODED FROM THE TOKEN IDS, never taken from the engine's
   `text` field. vLLM strips special tokens and the stop string from `text`, so
   offsets computed against it would drift. The names found are then checked
   against prompts.parse_completion on that same text, so this module and the
   scorer can never disagree about which tool was called.

Log-probs are the model's own (raw, before sampling processors). Decoding is
greedy, so the sampled token is the argmax and its log-prob is the largest.

Pure Python: the tokenizer comes in as a `decode` callable, so the core stays
free of transformers (tests/test_core_is_light.py).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from src.prompts import IM_END, parse_completion

Decode = Callable[[Sequence[int]], str]

# The opening of a Qwen tool call, up to the first character of the name string.
# Whitespace is allowed wherever json.loads allows it, so a compact `{"name":"x"`
# and the rendered `{"name": "x"` both match.
_NAME_OPEN = re.compile(r'<tool_call>\s*\{\s*"name"\s*:\s*"')

TOOL_CALL_TOKEN = "<tool_call>"


@dataclass(frozen=True)
class NameSpan:
    name: str
    start: int  # character offsets into the decoded completion, end exclusive
    end: int


@dataclass(frozen=True)
class StepConfidence:
    kind: str  # "tool_call" | "reply" | "unparsed"
    names: tuple[str, ...]
    name_confidence: float | None  # exp(mean name-token log-prob); min over calls
    name_logprob_mean: float | None  # the log-space value behind name_confidence
    name_tokens: int  # tokens counted, summed over calls
    p_call: float | None  # p(<tool_call>) at position 0 (an upper bound if bounded)
    p_call_bounded: bool  # True if <tool_call> was outside the recorded top-k
    decision_confidence: float | None
    step_confidence: float | None  # the number the threshold is applied to

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def hf_decoder(tokenizer: Any) -> Decode:
    """The `decode` to pass for a Hugging Face (or vLLM) tokenizer. Special
    tokens kept and spaces left alone, so the text is exactly what was generated."""
    return lambda ids: tokenizer.decode(
        list(ids), skip_special_tokens=False, clean_up_tokenization_spaces=False
    )


def name_spans(text: str) -> list[NameSpan]:
    """Where each tool name sits in `text`, in order. Reads the JSON string
    itself, so an escaped quote inside a name cannot end the span early."""
    spans = []
    for m in _NAME_OPEN.finditer(text):
        start = m.end()
        try:
            # raw_decode reads exactly one JSON string starting at the quote.
            name, end = json.JSONDecoder().raw_decode(text, start - 1)
        except json.JSONDecodeError:
            continue
        if isinstance(name, str):
            spans.append(NameSpan(name, start, end - 1))  # end - 1: drop the closing quote
    return spans


def token_char_ends(decode: Decode, token_ids: Sequence[int], upto: int) -> list[int]:
    """End offset of each token in decode(token_ids), stopping once `upto`
    characters are covered. Prefix decoding (rather than decoding tokens one by
    one) keeps multi-byte characters split across tokens from shifting offsets."""
    ends = []
    for i in range(len(token_ids)):
        end = len(decode(token_ids[: i + 1]))
        ends.append(end)
        if end >= upto:
            break
    return ends


def call_probability(
    first_alternatives: Mapping[int, float], call_token_id: int
) -> tuple[float, bool]:
    """p(<tool_call>) at the first position, from the top-k log-probs recorded
    there. Returns (p, bounded): bounded means <tool_call> was not in the top-k,
    and p is the k-th probability, an upper bound on the true value."""
    if not first_alternatives:
        raise ValueError("no first-position alternatives recorded")
    if call_token_id in first_alternatives:
        return math.exp(first_alternatives[call_token_id]), False
    return math.exp(min(first_alternatives.values())), True


def score_completion(
    token_ids: Sequence[int],
    token_logprobs: Sequence[float],
    decode: Decode,
    *,
    first_alternatives: Mapping[int, float] | None,
    call_token_id: int,
) -> StepConfidence:
    """Confidence of one generated turn. `token_logprobs[i]` is the log-prob of
    `token_ids[i]`, the token the model actually emitted; `first_alternatives`
    maps token id -> log-prob for the top-k candidates at position 0."""
    if len(token_ids) != len(token_logprobs):
        raise ValueError(f"{len(token_ids)} token ids but {len(token_logprobs)} log-probs")
    text = decode(token_ids).split(IM_END, 1)[0]
    parsed = parse_completion(text)
    unparsed = StepConfidence("unparsed", (), None, None, 0, None, False, None, None)
    if parsed.action is None:
        return unparsed

    p_call, bounded = (None, False)
    if first_alternatives:
        p_call, bounded = call_probability(first_alternatives, call_token_id)

    if parsed.action.kind == "reply":
        decision = None if p_call is None else 1.0 - p_call
        return StepConfidence("reply", (), None, None, 0, p_call, bounded, decision, decision)

    names = tuple(c.name for c in parsed.action.calls)
    spans = name_spans(text)
    if tuple(s.name for s in spans) != names:
        # The regex and the parser disagree about which tools were called. Never
        # guess a span: report the turn as having no measurable name.
        return unparsed

    ends = token_char_ends(decode, token_ids, spans[-1].end)
    starts = [0, *ends[:-1]]
    means, counted = [], 0
    for span in spans:
        # Token i covers characters [starts[i], ends[i]); keep it if that
        # overlaps the name's [span.start, span.end).
        picked = [
            token_logprobs[i]
            for i in range(len(ends))
            if starts[i] < span.end and ends[i] > span.start
        ]
        if not picked:
            return unparsed
        means.append(sum(picked) / len(picked))
        counted += len(picked)
    worst = min(means)
    name_conf = math.exp(worst)
    step = None if p_call is None else min(p_call, name_conf)
    return StepConfidence(
        "tool_call", names, name_conf, worst, counted, p_call, bounded, p_call, step
    )
