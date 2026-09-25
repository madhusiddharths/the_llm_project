"""Step confidence: the properties that would fail silently.

  - only the name's tokens count, never the JSON around it or the arguments,
  - the mean is length-neutral (a long name is not "less sure" than a short one),
  - a token fused with a quote counts only if it carries name characters,
  - several calls in one turn take the least confident,
  - the call-vs-reply decision is p(<tool_call>), never the reply's first word,
  - the step is the min of name and decision,
  - unparseable turns get no confidence rather than a made-up one.

Most tests use a one-character-per-token fake, where every offset is obvious
and '<' stands in for the <tool_call> token. The last group uses the real
Qwen2.5 tokenizer when it is in the local cache.
"""

from __future__ import annotations

import math

import pytest

from src.logprobs import TOOL_CALL_TOKEN, call_probability, hf_decoder, name_spans
from src.logprobs import score_completion as _score
from src.prompts import IM_END

CALL_ID = ord("<")  # the fake tokenizer's stand-in for <tool_call>


def chars(text: str) -> list[int]:
    return [ord(c) for c in text]


def decode(ids) -> str:
    return "".join(chr(i) for i in ids)


def score_completion(ids, lp, dec, *, alts=None, call_id=CALL_ID):
    """Defaults the first-position alternatives to "the first token was certain"."""
    if alts is None and ids:
        alts = {ids[0]: lp[0]}
    return _score(ids, lp, dec, first_alternatives=alts, call_token_id=call_id)


def logprobs_for(text: str, low: dict[str, float], default: float = 0.0) -> list[float]:
    """One log-prob per character: `low[s]` for every character inside each
    occurrence of the substring s, `default` elsewhere."""
    lp = [default] * len(text)
    for sub, value in low.items():
        start = text.index(sub)
        for i in range(start, start + len(sub)):
            lp[i] = value
    return lp


CALL = '<tool_call>\n{"name": "get_order_details", "arguments": {"order_id": "#W1"}}\n</tool_call>'


def test_the_span_is_the_name_and_nothing_else():
    (span,) = name_spans(CALL)
    assert CALL[span.start : span.end] == "get_order_details"


def test_only_name_tokens_move_the_confidence():
    # Every non-name token is made very unsure; the name tokens sit at p=0.5.
    lp = logprobs_for(CALL, {"get_order_details": math.log(0.5)}, default=math.log(0.01))
    conf = score_completion(chars(CALL), lp, decode)
    assert conf.kind == "tool_call" and conf.names == ("get_order_details",)
    assert conf.name_confidence == pytest.approx(0.5)
    assert conf.name_tokens == len("get_order_details")


def test_mean_not_sum_so_name_length_does_not_matter():
    short = '<tool_call>\n{"name": "calculate", "arguments": {}}\n</tool_call>'
    long = '<tool_call>\n{"name": "find_user_id_by_name_zip", "arguments": {}}\n</tool_call>'
    a = score_completion(chars(short), logprobs_for(short, {"calculate": -0.2}), decode)
    b = score_completion(
        chars(long), logprobs_for(long, {"find_user_id_by_name_zip": -0.2}), decode
    )
    assert a.name_confidence == pytest.approx(b.name_confidence) == pytest.approx(math.exp(-0.2))


def test_compact_json_and_escaped_quotes_are_read_as_json():
    text = '<tool_call>{"name":"we\\"ird","arguments":{}}</tool_call>'
    (span,) = name_spans(text)
    assert text[span.start : span.end] == 'we\\"ird'  # the raw characters, escape included
    assert span.name == 'we"ird'


def test_several_calls_take_the_least_confident():
    text = (
        '<tool_call>\n{"name": "get_user_details", "arguments": {}}\n</tool_call>\n'
        '<tool_call>\n{"name": "get_order_details", "arguments": {}}\n</tool_call>'
    )
    lp = logprobs_for(text, {"get_user_details": math.log(0.9), "get_order_details": math.log(0.4)})
    conf = score_completion(chars(text), lp, decode)
    assert conf.names == ("get_user_details", "get_order_details")
    assert conf.name_confidence == pytest.approx(0.4)
    assert conf.name_tokens == len("get_user_details") + len("get_order_details")


def test_a_call_is_as_sure_as_its_weaker_half():
    lp = logprobs_for(CALL, {"get_order_details": math.log(0.9)})
    # p(<tool_call>) = 0.6 at the first position: the decision is the weak half.
    conf = score_completion(chars(CALL), lp, decode, alts={CALL_ID: math.log(0.6), 73: -1.0})
    assert conf.decision_confidence == pytest.approx(0.6)
    assert conf.step_confidence == pytest.approx(0.6)
    # Now the name is the weak half.
    lp = logprobs_for(CALL, {"get_order_details": math.log(0.3)})
    conf = score_completion(chars(CALL), lp, decode, alts={CALL_ID: math.log(0.95)})
    assert conf.step_confidence == pytest.approx(0.3)


def test_a_reply_is_measured_by_p_call_not_by_its_first_word():
    text = "Could you confirm your email?" + IM_END
    # The first word itself is unsure (0.2): many ways to open a reply. That must
    # not matter. What matters is p(<tool_call>) = 0.1, so the reply is 0.9 sure.
    lp = [math.log(0.2)] + [0.0] * (len(text) - 1)
    alts = {ord("C"): math.log(0.2), ord("I"): math.log(0.2), CALL_ID: math.log(0.1)}
    conf = score_completion(chars(text), lp, decode, alts=alts)
    assert conf.kind == "reply" and conf.name_confidence is None and conf.name_tokens == 0
    assert conf.p_call == pytest.approx(0.1) and not conf.p_call_bounded
    assert conf.decision_confidence == pytest.approx(0.9)
    assert conf.step_confidence == pytest.approx(0.9)


def test_a_reply_torn_with_calling_is_unsure():
    text = "Your order is cancelled." + IM_END
    alts = {ord("Y"): math.log(0.5), CALL_ID: math.log(0.45)}
    conf = score_completion(
        chars(text), [math.log(0.5)] + [0.0] * (len(text) - 1), decode, alts=alts
    )
    assert conf.step_confidence == pytest.approx(0.55)


def test_tool_call_outside_the_top_k_uses_the_bound():
    # <tool_call> not among the recorded candidates: p_call <= the smallest one.
    p, bounded = call_probability({1: math.log(0.7), 2: math.log(0.04)}, CALL_ID)
    assert bounded and p == pytest.approx(0.04)
    p, bounded = call_probability({CALL_ID: math.log(0.3), 2: math.log(0.5)}, CALL_ID)
    assert not bounded and p == pytest.approx(0.3)
    with pytest.raises(ValueError):
        call_probability({}, CALL_ID)


def test_no_alternatives_recorded_means_no_step_confidence():
    conf = _score(
        chars(CALL), [0.0] * len(CALL), decode, first_alternatives=None, call_token_id=CALL_ID
    )
    assert conf.name_confidence == pytest.approx(1.0) and conf.step_confidence is None


@pytest.mark.parametrize(
    "text",
    [
        '<tool_call>\n{"name": "get_user_details", "arguments": {',  # cut off mid-call
        '<tool_call>\n{"name": 7, "arguments": {}}\n</tool_call>',  # name not a string
        "",
    ],
)
def test_an_unparseable_turn_gets_no_confidence(text):
    conf = score_completion(chars(text), [0.0] * len(text), decode)
    assert conf.kind == "unparsed" and conf.step_confidence is None


def test_text_after_im_end_is_ignored():
    text = CALL + IM_END + '<tool_call>\n{"name": "x", "arguments": {}}\n</tool_call>'
    lp = logprobs_for(text, {"get_order_details": math.log(0.8)})
    conf = score_completion(chars(text), lp, decode)
    assert conf.names == ("get_order_details",)
    assert conf.name_confidence == pytest.approx(0.8)


def test_mismatched_lengths_are_refused():
    with pytest.raises(ValueError):
        score_completion(chars(CALL), [0.0], decode)


# --- the real Qwen2.5 tokenizer --------------------------------------------------


@pytest.fixture(scope="module")
def qwen():
    transformers = pytest.importorskip("transformers")
    try:
        return transformers.AutoTokenizer.from_pretrained(
            "Qwen/Qwen2.5-0.5B-Instruct", local_files_only=True
        )
    except Exception:
        pytest.skip("Qwen tokenizer not in the local HF cache")


def _pieces(tok, text):
    ids = tok(text, add_special_tokens=False).input_ids
    return ids, [tok.decode([i]) for i in ids]


def test_qwen_counts_exactly_the_name_tokens(qwen):
    ids, pieces = _pieces(qwen, CALL + IM_END)
    name_idx = [i for i, p in enumerate(pieces) if p in ("get", "_order", "_details")]
    assert len(name_idx) == 3  # the tokenizer splits the name the way this test assumes
    lp = [math.log(0.01)] * len(ids)
    for i, v in zip(name_idx, (math.log(0.9), math.log(0.6), math.log(0.99)), strict=True):
        lp[i] = v
    call_id = qwen.convert_tokens_to_ids(TOOL_CALL_TOKEN)
    assert ids[0] == call_id  # one token: p(<tool_call>) is the whole call decision
    conf = score_completion(
        ids, lp, hf_decoder(qwen), alts={call_id: math.log(0.97)}, call_id=call_id
    )
    assert conf.name_tokens == 3
    expected = math.exp((math.log(0.9) + math.log(0.6) + math.log(0.99)) / 3)
    assert conf.name_confidence == pytest.approx(expected)
    assert conf.decision_confidence == pytest.approx(0.97)
    assert conf.step_confidence == pytest.approx(expected)


def test_qwen_fused_quote_tokens_are_left_out(qwen):
    # Compact JSON makes Qwen fuse the quotes into '":"' and '","' tokens.
    text = '<tool_call>\n{"name":"users_lookupByEmail","arguments":{}}\n</tool_call>'
    ids, pieces = _pieces(qwen, text)
    assert '":"' in pieces and '","' in pieces
    lp = [0.0] * len(ids)
    lp[pieces.index('":"')] = lp[pieces.index('","')] = math.log(0.01)
    call_id = qwen.convert_tokens_to_ids(TOOL_CALL_TOKEN)
    conf = score_completion(ids, lp, hf_decoder(qwen), call_id=call_id)
    assert conf.name_confidence == pytest.approx(1.0)
    assert conf.name_tokens == 3  # users, _lookup, ByEmail


# --- generate.py: picking the emitted token's log-prob out of vLLM's output -------


class _Lp:
    def __init__(self, logprob):
        self.logprob = logprob


def test_vllm_log_probs_are_looked_up_by_the_emitted_token_id():
    from src.generate import sampled_logprobs

    # The dict order puts a different token first; the lookup must not care.
    per_step = [{9: _Lp(-5.0), 1: _Lp(-0.1)}, {2: _Lp(-0.2)}]
    assert sampled_logprobs([1, 2], per_step) == [-0.1, -0.2]
    with pytest.raises(ValueError):
        sampled_logprobs([1, 2], None)
    with pytest.raises(KeyError):
        sampled_logprobs([3, 2], per_step)


def test_first_position_candidates_are_kept_best_first():
    from src.generate import first_top

    assert first_top({9: _Lp(-5.0), 1: _Lp(-0.1), 4: _Lp(-2.0)}) == [
        [1, -0.1],
        [4, -2.0],
        [9, -5.0],
    ]
