"""The NL-assertion judge, pointed at a provider we actually hold a key for.

tau2-bench scores 40 of the 114 retail tasks with an LLM judge, and hardcodes
that judge to gpt-4.1 (tau2/config.py, DEFAULT_LLM_NL_ASSERTIONS). We have no
OpenAI key, so the judge raises, the whole episode is discarded after three
attempts, and a third of the training split silently produces nothing.

MEASURED 2026-09-10, batch 0 of the first real harvest: tasks 63 and 108 failed
exactly this way; 79 and 22, which carry no nl_assertions, scored normally. The
split is 18/54 train tasks and 22/60 eval tasks, so week 3's baseline hits it too.

Dropping the judge instead (EvaluationType.ALL_IGNORE_BASIS) is not the cheap
way out it looks like. NL_ASSERTION sits in reward_basis for 112 of the 114
tasks, so removing it deletes a multiplicative factor and pushes rewards UP.
Under rejection sampling that admits trajectories which failed their qualitative
check — it would quietly lower the quality of the training data, which is the
one thing week 1 exists to produce.

So the judge stays, on OpenRouter, and is frozen exactly like the simulator:
the same grader in the week 1 harvest and the week 3 student runs, or the two
are scored by different graders and the comparison is void. That is why judge
is part of ExperimentConfig.teacher_fingerprint().
"""

from __future__ import annotations

import re

from src.config import JudgeConfig

# tau2 feeds the judge's reply straight to json.loads. gpt-4.1 returns bare JSON;
# open models very often wrap it in a markdown fence. One fence is the difference
# between a scored episode and a discarded one, so strip it before tau2 parses.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def strip_fences(text: str) -> str:
    """Unwrap a ```json ... ``` fence. Text without one is returned untouched."""
    match = _FENCE.match(text)
    return match.group(1) if match else text


def install(judge: JudgeConfig) -> None:
    """Point tau2's NL-assertion judge at our model. Idempotent.

    Patches the evaluator module rather than tau2.config: evaluator_nl_assertions
    does `from tau2.config import DEFAULT_LLM_NL_ASSERTIONS`, so the name is bound
    into that namespace at import and rebinding tau2.config would do nothing.
    The constants are read at call time from module globals, so this takes effect
    for every judged episode after it runs.

    Judge calls go through litellm.completion like every other call, so
    throttle.install already paces and counts them — one extra request per
    episode that has nl_assertions, ~54 across the whole week 1 harvest.
    """
    from tau2.evaluator import evaluator_nl_assertions as nl

    nl.DEFAULT_LLM_NL_ASSERTIONS = judge.model
    nl.DEFAULT_LLM_NL_ASSERTIONS_ARGS = {"temperature": judge.temperature}

    if getattr(nl.generate, "_judge_patched", False):
        return

    inner = nl.generate

    def generate_and_unfence(*args, **kwargs):
        message = inner(*args, **kwargs)
        content = getattr(message, "content", None)
        if isinstance(content, str):
            cleaned = strip_fences(content)
            if cleaned != content:
                message = message.model_copy(update={"content": cleaned})
        return message

    generate_and_unfence._judge_patched = True
    nl.generate = generate_and_unfence
