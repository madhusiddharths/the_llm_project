"""Free-running plumbing: what the student's completion becomes inside tau2.

The pure pieces run everywhere. The integration test drives StudentAgent
against tau2's real retail environment (no user simulator, no API calls): a
user turn, the student's tool call, the environment's result, the student's
reply. It is skipped where tau2 is not installed (CI).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.eval_free import (
    EMPTY_REPLY,
    OVERFLOW_REPLY,
    decide_turn,
    episode_stats,
    history_dicts,
)
from src.trajectories import DEGENERATE_USER_CHARS, TAU2_GREETING


def test_a_tool_call_completion_becomes_a_tool_call_with_an_id():
    turn = decide_turn(
        '<tool_call>\n{"name": "get_order_details", "arguments": {"order_id": "#W1"}}\n</tool_call><|im_end|>'
    )
    assert turn.content is None
    assert turn.tool_calls[0]["name"] == "get_order_details"
    assert turn.tool_calls[0]["arguments"] == {"order_id": "#W1"}
    assert turn.tool_calls[0]["id"].startswith("call_")
    assert turn.raw["parse_error"] is None


def test_a_reply_completion_becomes_text():
    turn = decide_turn("Could I have your email?<|im_end|>")
    assert (turn.content, turn.tool_calls) == ("Could I have your email?", [])


def test_an_unparseable_completion_is_sent_as_text_and_recorded():
    turn = decide_turn("<tool_call>\n{broken\n</tool_call>")
    assert turn.content == "<tool_call>\n{broken\n</tool_call>"
    assert turn.raw["parse_error"] == "tool call is not valid JSON"


def test_an_empty_completion_still_yields_a_valid_message():
    turn = decide_turn("   <|im_end|>")
    assert turn.content == EMPTY_REPLY and turn.raw["parse_error"] == "empty completion"


def test_context_overflow_ends_politely_and_is_flagged():
    turn = decide_turn(None, overflow=True)
    assert turn.content == OVERFLOW_REPLY and turn.raw["context_overflow"]


def test_history_dicts_match_the_serializer_shape():
    msgs = [
        SimpleNamespace(role="assistant", content=TAU2_GREETING, tool_calls=None),
        SimpleNamespace(
            role="assistant",
            content=None,
            tool_calls=[SimpleNamespace(name="f", arguments={"x": 1})],
        ),
        SimpleNamespace(role="tool", content="ok"),  # no tool_calls attribute at all
    ]
    assert history_dicts(msgs) == [
        {"role": "assistant", "content": TAU2_GREETING, "tool_calls": None},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"name": "f", "arguments": {"x": 1}}],
        },
        {"role": "tool", "content": "ok", "tool_calls": None},
    ]


def test_episode_stats_count_what_went_wrong():
    msgs = [
        {"role": "assistant", "content": TAU2_GREETING},
        {"role": "user", "content": "x" * (DEGENERATE_USER_CHARS + 1)},
        {"role": "assistant", "tool_calls": [{"name": "native"}], "raw_data": {}},
        {"role": "assistant", "tool_calls": [{"name": "distractor"}], "raw_data": {}},
        {"role": "assistant", "tool_calls": [{"name": "made_up"}], "raw_data": {}},
        {"role": "assistant", "content": "{bad", "raw_data": {"parse_error": "x"}},
        {"role": "assistant", "content": "sorry", "raw_data": {"context_overflow": True}},
    ]
    s = episode_stats(msgs, {"native", "distractor"}, {"native"})
    assert s == {
        "student_turns": 5,
        "escalated_turns": 0,
        "tool_calls": 3,
        "parse_errors": 1,
        "context_overflows": 1,
        "distractor_calls": 1,
        "hallucinated_calls": 1,
        "degenerate_user_turn": True,
    }


# --- integration with tau2 (no API) --------------------------------------------


class Scripted:
    """A policy that returns canned completions and records every prompt."""

    name = "scripted"

    def __init__(self, completions):
        self.completions = list(completions)
        self.prompts = []

    def fits(self, prompt):
        return True

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.completions.pop(0)


def test_student_agent_round_trip_through_the_real_retail_environment():
    pytest.importorskip("tau2")
    from tau2.data_model.message import UserMessage
    from tau2.registry import registry

    from src.catalogs import load_catalog, load_system_prompt
    from src.config import load_config
    from src.eval_free import register_student_agent
    from src.prompts import serialize_state

    cfg = load_config("configs/qwen05b.yaml")
    system, native = load_system_prompt(cfg), load_catalog(cfg, 16)
    catalog = load_catalog(cfg, 80)
    policy = Scripted(
        [
            '<tool_call>\n{"name": "find_user_id_by_email", "arguments": {"email": "nobody@example.com"}}\n</tool_call>',
            "I couldn't find that account. Could you check the email?",
        ]
    )
    size = 80_001  # a name no other test registers
    name = register_student_agent(size, catalog, native, system, policy)
    env = registry.get_env_constructor("retail")()
    agent = registry.get_agent_factory(name)(tools=env.get_tools(), domain_policy=env.get_policy())

    from tau2.orchestrator.orchestrator import DEFAULT_FIRST_AGENT_MESSAGE

    state = agent.get_init_state(message_history=[DEFAULT_FIRST_AGENT_MESSAGE])
    user = UserMessage(role="user", content="Hi, my email is nobody@example.com")
    call_msg, state = agent.generate_next_message(user, state)
    assert call_msg.tool_calls[0].name == "find_user_id_by_email"
    assert call_msg.raw_data["parse_error"] is None

    # The prompt the student saw is exactly the shared serializer's rendering.
    expected = serialize_state(
        system=system,
        tools=catalog,
        messages=[
            {"role": "assistant", "content": TAU2_GREETING, "tool_calls": None},
            {"role": "user", "content": user.content, "tool_calls": None},
        ],
    )
    assert policy.prompts[0] == expected

    result = env.get_response(call_msg.tool_calls[0])  # the environment executes it
    assert result.error  # no such user in the retail DB
    reply, state = agent.generate_next_message(result, state)
    assert reply.content.startswith("I couldn't find") and not reply.tool_calls
    assert "<tool_response>" in policy.prompts[1]


def test_a_different_tau2_policy_is_refused():
    pytest.importorskip("tau2")
    from tau2.registry import registry

    from src.catalogs import load_catalog, load_system_prompt
    from src.config import load_config
    from src.eval_free import register_student_agent
    from src.invariants import InvariantViolation

    cfg = load_config("configs/qwen05b.yaml")
    name = register_student_agent(
        80_002, load_catalog(cfg, 16), load_catalog(cfg, 16), load_system_prompt(cfg), Scripted([])
    )
    env = registry.get_env_constructor("retail")()
    with pytest.raises(InvariantViolation, match="system prompt"):
        registry.get_agent_factory(name)(tools=env.get_tools(), domain_policy="a different policy")


def test_a_whole_episode_runs_through_tau2s_orchestrator_offline(tmp_path):
    """run_tasks end to end: the orchestrator, the student agent, the retail
    environment and tau2's scoring. A scripted user replaces the simulator and
    the task has no NL assertions, so nothing calls an API."""
    pytest.importorskip("tau2")
    from tau2.data_model.message import UserMessage
    from tau2.registry import registry
    from tau2.runner.batch import run_tasks
    from tau2.user.user_simulator import UserSimulator

    from src.catalogs import load_catalog, load_system_prompt
    from src.config import load_config
    from src.eval_free import build_student_run_config, register_student_agent
    from src.harvest import episode_reward

    lines = ("Hi, I want to check my account. My email is nobody@example.com", "###STOP###")

    class ScriptedUser(UserSimulator):
        def _generate_next_message(self, message, state):
            return UserMessage(role="user", content=lines[min(len(state.messages) // 2, 1)])

    if "scripted_user_test" not in registry._users:
        registry.register_user(ScriptedUser, "scripted_user_test")

    cfg = load_config("configs/qwen05b.yaml")
    policy = Scripted(
        [
            '<tool_call>\n{"name": "find_user_id_by_email", "arguments": {"email": "nobody@example.com"}}\n</tool_call>',
            "I couldn't find an account with that email.",
        ]
        * 3
    )
    name = register_student_agent(
        80_003, load_catalog(cfg, 16), load_catalog(cfg, 16), load_system_prompt(cfg), policy
    )
    tasks = [
        t
        for t in registry.get_tasks_loader("retail")()
        if not (t.evaluation_criteria and t.evaluation_criteria.nl_assertions)
    ][:1]
    run_cfg = build_student_run_config(cfg, name, [str(tasks[0].id)], 0, tmp_path / "b.json")
    run_cfg = run_cfg.model_copy(update={"user": "scripted_user_test", "max_steps": 8})
    results = run_tasks(run_cfg, tasks, save_path=tmp_path / "b.json", console_display=False)

    sim = results.simulations[0]
    assert episode_reward(sim) is not None, sim.termination_reason  # scored, not an infra error
    msgs = [m.model_dump(mode="json") for m in sim.messages]
    assert msgs[0]["content"] == TAU2_GREETING
    names = [
        c["name"] for m in msgs if m["role"] == "assistant" for c in (m.get("tool_calls") or [])
    ]
    assert "find_user_id_by_email" in names
    stats = episode_stats(msgs, {"find_user_id_by_email"}, {"find_user_id_by_email"})
    assert stats["parse_errors"] == 0 and stats["student_turns"] >= 2
    assert (tmp_path / "b.json").exists()


def test_hub_pull_never_overwrites_a_local_results_file(tmp_path, monkeypatch):
    hub = pytest.importorskip("huggingface_hub")
    from src.eval_free import hub_pull

    def boom(**kwargs):
        raise AssertionError("must not download over a local file")

    monkeypatch.setattr(hub, "snapshot_download", boom)
    local = tmp_path / "results.jsonl"
    local.write_text("{}\n")
    hub_pull("someone/repo", local, tmp_path / "traj")  # returns without touching the Hub
    assert local.read_text() == "{}\n"


# --- the server backend and --concurrency ----------------------------------------


def _cfg():
    from src.config import load_config

    return load_config("configs/qwen15b.yaml")


def test_the_server_is_started_with_the_in_process_engine_settings():
    from src.serving import SERVED_NAME, server_command

    cfg = _cfg()
    cmd = server_command(cfg, "/adapters/sft", 32768, port=9000)
    flag = {cmd[i]: cmd[i + 1] for i in range(len(cmd) - 1) if cmd[i].startswith("--")}
    assert flag["--model"] == cfg.model.base_model
    assert flag["--dtype"] == "half" and flag["--max-model-len"] == "32768"
    assert flag["--gpu-memory-utilization"] == "0.9" and flag["--seed"] == str(cfg.seed)
    assert flag["--lora-modules"] == f"{SERVED_NAME}=/adapters/sft"
    assert flag["--max-lora-rank"] == str(cfg.train.lora_r) and "--enable-lora" in cmd
    # zero-shot: the bare model answers to the same name, no LoRA flags
    bare = server_command(cfg, None, 32768)
    assert "--enable-lora" not in bare and SERVED_NAME in bare


def test_a_server_request_is_greedy_with_the_teachers_budget_and_stop():
    from src.serving import completion_request, completion_text

    cfg = _cfg()
    body = completion_request(cfg, "PROMPT")
    assert body["prompt"] == "PROMPT" and body["temperature"] == 0.0
    assert body["max_tokens"] == cfg.teacher.max_tokens and body["stop"] == ["<|im_end|>"]
    assert body["seed"] == cfg.seed
    assert completion_text({"choices": [{"text": "hi"}]}) == "hi"
    with pytest.raises(ValueError):
        completion_text({"choices": []})


def test_the_server_and_in_process_vllm_share_a_results_identity():
    """1.5B c16's serial episodes must count toward the same cell on the server."""
    from src.eval_free import results_engine

    assert results_engine("server") == results_engine("vllm") == "vllm"
    assert results_engine("hf") == "hf" and results_engine("fixed") == "fixed"


@pytest.mark.parametrize("backend", ["vllm", "hf"])
def test_concurrency_is_refused_for_an_in_process_engine(backend):
    from src.eval_free import main

    with pytest.raises(SystemExit):
        main(["--backend", backend, "--concurrency", "4", "--smoke"])


def test_several_episodes_run_at_once_through_tau2_offline(tmp_path):
    """max_concurrency > 1: tau2's worker threads share one student policy."""
    pytest.importorskip("tau2")
    import threading

    from tau2.data_model.message import UserMessage
    from tau2.registry import registry
    from tau2.runner.batch import run_tasks
    from tau2.user.user_simulator import UserSimulator

    from src.catalogs import load_catalog, load_system_prompt
    from src.config import load_config
    from src.eval_free import build_student_run_config, register_student_agent
    from src.harvest import episode_reward

    class StopUser(UserSimulator):
        def _generate_next_message(self, message, state):
            return UserMessage(role="user", content="###STOP###" if state.messages else "Hi")

    if "stop_user_test" not in registry._users:
        registry.register_user(StopUser, "stop_user_test")

    threads = set()

    class Recording:
        name = "recording"

        def fits(self, prompt):
            return True

        def __call__(self, prompt):
            threads.add(threading.get_ident())
            return "How can I help?"

    cfg = load_config("configs/qwen05b.yaml")
    name = register_student_agent(
        80_004, load_catalog(cfg, 16), load_catalog(cfg, 16), load_system_prompt(cfg), Recording()
    )
    tasks = [
        t
        for t in registry.get_tasks_loader("retail")()
        if not (t.evaluation_criteria and t.evaluation_criteria.nl_assertions)
    ][:3]
    ids = [str(t.id) for t in tasks]
    run_cfg = build_student_run_config(cfg, name, ids, 0, tmp_path / "b.json", concurrency=3)
    assert run_cfg.max_concurrency == 3
    run_cfg = run_cfg.model_copy(update={"user": "stop_user_test", "max_steps": 6})
    results = run_tasks(run_cfg, tasks, save_path=tmp_path / "b.json", console_display=False)
    assert sorted(str(s.task_id) for s in results.simulations) == sorted(ids)
    assert all(episode_reward(s) is not None for s in results.simulations)


# --- Stage D: escalation -------------------------------------------------------


def test_a_logprob_request_adds_top_k_by_token_id_and_nothing_else():
    from src.serving import FIRST_TOP_K, completion_request

    cfg = _cfg()
    plain, scored = completion_request(cfg, "P"), completion_request(cfg, "P", logprobs=True)
    assert scored == {**plain, "logprobs": FIRST_TOP_K, "return_tokens_as_token_ids": True}
    assert "logprobs" not in plain  # Stage A requests are unchanged


def test_a_logprob_server_reports_raw_logprobs():
    from src.serving import server_command

    cmd = server_command(_cfg(), None, 32768, logprobs=True)
    assert cmd[cmd.index("--logprobs-mode") + 1] == "raw_logprobs"
    assert "--logprobs-mode" not in server_command(_cfg(), None, 32768)


def test_a_scored_response_is_read_by_token_id():
    from src.serving import scored_completion

    body = {
        "choices": [
            {
                "text": "hi",
                "logprobs": {
                    "tokens": ["token_id:5", "token_id:7"],
                    "token_logprobs": [-0.1, -0.2],
                    "top_logprobs": [
                        {"token_id:5": -0.1, "token_id:9": -2.5},
                        {"token_id:7": -0.2},
                    ],
                },
            }
        ]
    }
    out = scored_completion(body)
    assert out.text == "hi" and out.token_ids == [5, 7] and out.token_logprobs == [-0.1, -0.2]
    assert out.first_alternatives == {5: -0.1, 9: -2.5}


def test_a_scored_response_without_token_ids_is_refused():
    from src.serving import scored_completion

    lp = {"tokens": ["hi"], "token_logprobs": [-0.1], "top_logprobs": [{"hi": -0.1}]}
    with pytest.raises(ValueError, match="return_tokens_as_token_ids"):
        scored_completion({"choices": [{"text": "hi", "logprobs": lp}]})
    lp = {"tokens": ["token_id:1"], "token_logprobs": [], "top_logprobs": []}
    with pytest.raises(ValueError, match="log-probs"):
        scored_completion({"choices": [{"text": "x", "logprobs": lp}]})


def test_a_stage_d_episode_is_budgeted_at_its_worst_case():
    from src.eval_free import escalated_episode_budget
    from src.harvest import MAX_REQUESTS_PER_EPISODE

    cfg = _cfg()
    assert escalated_episode_budget(cfg) == cfg.simulator.max_turns * 2 + 2
    assert escalated_episode_budget(cfg) > MAX_REQUESTS_PER_EPISODE


def test_escalation_is_refused_off_the_server_path():
    from src.eval_free import main

    with pytest.raises(SystemExit):
        main(["--backend", "vllm", "--escalate", "0.7", "--smoke"])


def test_escalated_turns_are_counted():
    msgs = [
        {"role": "assistant", "content": TAU2_GREETING},
        {"role": "assistant", "content": "a", "raw_data": {"escalated": True}},
        {"role": "assistant", "content": "b", "raw_data": {"escalated": False}},
        {"role": "assistant", "content": "c", "raw_data": {}},
    ]
    assert episode_stats(msgs, set(), set())["escalated_turns"] == 1


class Scoring:
    """A policy with fixed completions and confidences, in order."""

    name = "scoring"

    def __init__(self, turns):
        self.turns = list(turns)

    def fits(self, prompt):
        return True

    def score(self, prompt):
        from src.logprobs import StepConfidence

        text, step = self.turns.pop(0)
        kind = "tool_call" if text.startswith("<tool_call>") else "reply"
        return text, StepConfidence(kind, (), step, None, 1, step, False, step, step)


def test_a_low_confidence_turn_goes_to_the_teacher_with_the_catalogs_tools(monkeypatch):
    """Offline: tau2's generate is replaced, so nothing calls the API."""
    pytest.importorskip("tau2")
    import tau2.utils.llm_utils as llm_utils
    from tau2.data_model.message import AssistantMessage, UserMessage
    from tau2.orchestrator.orchestrator import DEFAULT_FIRST_AGENT_MESSAGE
    from tau2.registry import registry

    from src.catalogs import load_catalog, load_system_prompt
    from src.config import load_config
    from src.eval_free import Escalation, register_student_agent

    seen = {}

    def fake_generate(model, messages, tools=None, **kwargs):
        seen.update(model=model, tools=[t.name for t in tools], kwargs=kwargs, messages=messages)
        return AssistantMessage(role="assistant", content="Teacher here.", raw_data={"id": "r1"})

    monkeypatch.setattr(llm_utils, "generate", fake_generate)
    cfg = load_config("configs/qwen05b.yaml")
    system, native, catalog = load_system_prompt(cfg), load_catalog(cfg, 16), load_catalog(cfg, 80)
    policy = Scoring([("I am unsure.", 0.3), ("I am sure.", 0.9)])
    esc = Escalation(0.7, "openrouter/teacher", {"temperature": 0.0})
    name = register_student_agent(80, catalog, native, system, policy, esc)
    assert name == "student_c80_t0.7"
    env = registry.get_env_constructor("retail")()
    agent = registry.get_agent_factory(name)(tools=env.get_tools(), domain_policy=env.get_policy())
    state = agent.get_init_state(message_history=[DEFAULT_FIRST_AGENT_MESSAGE])

    low, state = agent.generate_next_message(UserMessage(role="user", content="Hi"), state)
    assert low.content == "Teacher here."
    assert low.raw_data["escalated"] and low.raw_data["route_reason"] == "low_confidence"
    assert low.raw_data["student_completion"] == "I am unsure."
    assert low.raw_data["step_confidence"] == 0.3 and low.raw_data["teacher_raw"] == {"id": "r1"}
    # The teacher saw the catalog the student saw, in its order, and tau2's own prompt.
    assert seen["model"] == "openrouter/teacher" and seen["kwargs"]["temperature"] == 0.0
    assert seen["tools"] == [t["function"]["name"] for t in catalog]
    assert seen["messages"][0].role == "system" and seen["messages"][-1].content == "Hi"

    high, state = agent.generate_next_message(UserMessage(role="user", content="Thanks"), state)
    assert high.content == "I am sure." and not high.raw_data["escalated"]
    assert high.raw_data["route_reason"] == "confident" and high.raw_data["parse_error"] is None
