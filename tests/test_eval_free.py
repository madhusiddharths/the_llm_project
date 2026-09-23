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
