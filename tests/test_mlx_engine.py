"""The MLX student: prefix-cache bookkeeping (pure), and, where MLX and the
0.5B model are available locally, that cache reuse never changes the output."""

from __future__ import annotations

import pytest

from src.mlx_engine import CachePool, common_prefix, reusable


def test_common_prefix_counts_matching_leading_tokens():
    assert common_prefix([1, 2, 3], [1, 2, 4]) == 2
    assert common_prefix([1, 2], [1, 2, 3]) == 2
    assert common_prefix([], [1]) == 0 and common_prefix([5], [6]) == 0


def test_an_episode_continues_from_its_own_cache():
    pool = CachePool(2)
    a, b = pool.slots
    a.tokens, b.tokens = [9, 9, 1, 1], [9, 9, 2, 2]
    slot, shared = pool.choose([9, 9, 2, 2, 5])
    assert slot is b and shared == 4


def test_a_new_episode_takes_the_least_recently_used_slot_sharing_the_system_prefix():
    pool = CachePool(2)
    a, b = pool.slots
    a.tokens, b.tokens = [9, 9, 1], [9, 9, 2]
    a.used, b.used = 10.0, 5.0  # b is older
    slot, shared = pool.choose([9, 9, 3])
    assert slot is b and shared == 2


def test_with_nothing_shared_an_empty_slot_is_used_first():
    pool = CachePool(2)
    a, _ = pool.slots
    a.tokens, a.used = [1, 2], 1.0
    slot, shared = pool.choose([7, 8])
    assert slot is not a and shared == 0


def test_at_least_one_prompt_token_is_always_recomputed():
    assert reusable(shared=10, prompt_len=10) == 9
    assert reusable(shared=4, prompt_len=10) == 4


def test_forced_eval_runs_each_episode_in_step_order():
    from src.generate import episode_order

    rows = [{"step_id": s} for s in ("7:0:12", "3:1:4", "7:0:2", "3:1:10")]
    assert [r["step_id"] for r in episode_order(rows)] == ["3:1:4", "3:1:10", "7:0:2", "7:0:12"]


# --- with the real engine (Apple silicon, 0.5B in the local HF cache) -------------


def _cached(repo: str) -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache

        return isinstance(try_to_load_from_cache(repo, "config.json"), str)
    except Exception:
        return False


BASE = "Qwen/Qwen2.5-0.5B-Instruct"


@pytest.fixture(scope="module")
def engine():
    pytest.importorskip("mlx_lm")
    if not _cached(BASE):
        pytest.skip(f"{BASE} is not in the local Hugging Face cache")
    from src.mlx_engine import MLXEngine

    return MLXEngine(BASE, max_tokens=24, pool_size=2)


def _prompt(user: str) -> str:
    from src.prompts import serialize_state

    return serialize_state(
        system="You are a helpful retail assistant.",
        tools=[],
        messages=[{"role": "user", "content": user, "tool_calls": None}],
    )


def test_a_reused_cache_gives_the_same_tokens_as_a_fresh_one(engine):
    from src.mlx_engine import MLXEngine

    first = _prompt("Hi, I want to return an order.")
    longer = first + "Sure, which order?<|im_end|>\n<|im_start|>user\nOrder #W123.<|im_end|>\n"
    longer += "<|im_start|>assistant\n"
    engine.generate(first)
    warm = engine.generate(longer)
    assert warm.reused_tokens > 0
    cold = MLXEngine(BASE, max_tokens=24).generate(longer)
    assert warm.token_ids == cold.token_ids and warm.reused_tokens > cold.reused_tokens == 0
    assert (
        max(abs(a - b) for a, b in zip(warm.token_logprobs, cold.token_logprobs, strict=True))
        < 0.02
    )


def test_a_generation_records_what_logprobs_scoring_needs(engine):
    g = engine.generate(_prompt("Hello"))
    assert len(g.token_ids) == len(g.token_logprobs) > 0
    assert len(g.first_alternatives) == 20 and g.token_ids[0] in g.first_alternatives
    assert g.finish_reason in ("stop", "length") and "<|im_end|>" not in g.text


def test_two_episodes_run_at_once_through_tau2_on_mlx(tmp_path, engine):
    """Offline: scripted users, a task with no NL assertions, so no API call."""
    pytest.importorskip("tau2")
    from tau2.data_model.message import UserMessage
    from tau2.registry import registry
    from tau2.runner.batch import run_tasks
    from tau2.user.user_simulator import UserSimulator

    from src.catalogs import load_catalog, load_system_prompt
    from src.config import load_config
    from src.eval_free import MLXPolicy, build_student_run_config, register_student_agent
    from src.harvest import episode_reward

    class StopUser(UserSimulator):
        def _generate_next_message(self, message, state):
            text = "###STOP###" if len(state.messages) > 2 else "Hi, what can you do?"
            return UserMessage(role="user", content=text)

    if "mlx_stop_user_test" not in registry._users:
        registry.register_user(StopUser, "mlx_stop_user_test")

    cfg = load_config("configs/qwen05b.yaml")
    policy = MLXPolicy.__new__(MLXPolicy)  # reuse the loaded engine, skip the merge
    policy.engine, policy.limit = engine, 30_000
    policy.call_token_id = engine.tokenizer.convert_tokens_to_ids("<tool_call>")
    name = register_student_agent(
        80_010, load_catalog(cfg, 16), load_catalog(cfg, 16), load_system_prompt(cfg), policy
    )
    tasks = [
        t
        for t in registry.get_tasks_loader("retail")()
        if not (t.evaluation_criteria and t.evaluation_criteria.nl_assertions)
    ][:2]
    ids = [str(t.id) for t in tasks]
    run_cfg = build_student_run_config(cfg, name, ids, 0, tmp_path / "b.json", concurrency=2)
    run_cfg = run_cfg.model_copy(update={"user": "mlx_stop_user_test", "max_steps": 6})
    results = run_tasks(run_cfg, tasks, save_path=tmp_path / "b.json", console_display=False)
    assert sorted(str(s.task_id) for s in results.simulations) == sorted(ids)
    assert all(episode_reward(s) is not None for s in results.simulations)


def test_engine_check_passes_at_95_percent_identical():
    from src.engine_check import compare

    a = {str(i): {"completion": "x"} for i in range(20)}
    b = {**{str(i): {"completion": " x\n"} for i in range(19)}, "19": {"completion": "y"}}
    out = compare(a, b)
    assert out["identical"] == 19 and out["passes"] and out["differing_steps"] == ["19"]
    b["18"] = {"completion": "z"}
    assert not compare(a, b)["passes"]
