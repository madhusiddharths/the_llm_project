"""Regression tests for the failure that cost a third of the 2026-09-10 harvest.

tau2 hardcodes its NL-assertion judge to gpt-4.1. We hold no OpenAI key, so
every task carrying nl_assertions raised at scoring time and was thrown away.
src/judge.py repoints it; these tests hold that repointing in place.
"""

from __future__ import annotations

import pytest

from src.config import load_config
from src.judge import install, strip_fences

REAL = "configs/qwen05b.yaml"


# --- fence stripping (pure; runs everywhere) --------------------------------


def test_bare_json_is_untouched():
    assert strip_fences('{"results": []}') == '{"results": []}'


@pytest.mark.parametrize("fence", ["```json", "```"])
def test_a_fenced_reply_is_unwrapped(fence):
    assert strip_fences(f'{fence}\n{{"results": []}}\n```') == '{"results": []}'


def test_unwrapped_output_is_valid_json():
    """The point of the strip: tau2 feeds this straight to json.loads."""
    import json

    fenced = '```json\n{"results": [{"metExpectation": true}]}\n```'
    assert json.loads(strip_fences(fenced))["results"][0]["metExpectation"] is True


def test_a_fence_inside_the_body_is_not_treated_as_the_wrapper():
    body = '{"reasoning": "the agent wrote ```code``` here"}'
    assert strip_fences(body) == body


# --- the patch itself (needs tau2, which CI does not install) ---------------


@pytest.fixture
def nl_module():
    pytest.importorskip("tau2", reason="tau2-bench is not installed in the dev environment")
    from tau2.evaluator import evaluator_nl_assertions as nl

    saved = (nl.DEFAULT_LLM_NL_ASSERTIONS, nl.DEFAULT_LLM_NL_ASSERTIONS_ARGS, nl.generate)
    yield nl
    nl.DEFAULT_LLM_NL_ASSERTIONS, nl.DEFAULT_LLM_NL_ASSERTIONS_ARGS, nl.generate = saved


def test_tau2_really_does_default_to_a_model_we_cannot_call(nl_module):
    """Guard the guard. If upstream ever fixes this, the patch can go."""
    assert "gpt" in nl_module.DEFAULT_LLM_NL_ASSERTIONS.lower()


def test_install_repoints_the_judge(nl_module):
    cfg = load_config(REAL)
    install(cfg.judge)
    assert cfg.judge.model == nl_module.DEFAULT_LLM_NL_ASSERTIONS
    assert {"temperature": cfg.judge.temperature} == nl_module.DEFAULT_LLM_NL_ASSERTIONS_ARGS


def test_install_unfences_what_the_judge_returns(nl_module):
    from tau2.data_model.message import AssistantMessage

    nl_module.generate = lambda *a, **k: AssistantMessage(
        role="assistant", content='```json\n{"results": []}\n```'
    )
    install(load_config(REAL).judge)
    assert nl_module.generate(model="x", messages=[]).content == '{"results": []}'


def test_the_judge_call_actually_uses_our_model(nl_module):
    """The end-to-end claim, without spending a request.

    This is why src/judge.py patches the evaluator module and not tau2.config:
    evaluator_nl_assertions did `from tau2.config import DEFAULT_LLM_NL_ASSERTIONS`,
    so the name is bound in ITS namespace. If a future refactor moved the lookup,
    the patch would go silently dead and 40 tasks would start failing again.
    """
    from tau2.data_model.message import AssistantMessage, UserMessage
    from tau2.evaluator.evaluator_nl_assertions import NLAssertionsEvaluator

    seen = {}

    def fake_generate(model, messages, **kwargs):
        seen["model"] = model
        seen["temperature"] = kwargs.get("temperature")
        return AssistantMessage(
            role="assistant",
            content='```json\n{"results": [{"expectedOutcome": "refunded",'
            ' "reasoning": "it was", "metExpectation": true}]}\n```',
        )

    nl_module.generate = fake_generate
    cfg = load_config(REAL)
    install(cfg.judge)

    checks = NLAssertionsEvaluator.evaluate_nl_assertions(
        [UserMessage(role="user", content="did you refund me?")], ["refunded"]
    )

    assert seen["model"] == cfg.judge.model
    assert seen["temperature"] == cfg.judge.temperature
    # and the fenced reply still parsed, which it would not have unpatched
    assert [c.met for c in checks] == [True]


def test_install_is_idempotent(nl_module):
    """harvest.py may call install once per process; a second call must not
    stack another wrapper around the first."""
    judge = load_config(REAL).judge
    install(judge)
    once = nl_module.generate
    install(judge)
    assert nl_module.generate is once
