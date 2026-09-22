"""Data card arithmetic, on synthetic episodes. No tau2, no cached files."""

from __future__ import annotations

from src.data_card import build_card, pct, render_markdown
from src.trajectories import TAU2_GREETING, Episode


def _schema(name):
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def _episode(task_id, rep, tool):
    messages = (
        {"role": "assistant", "content": TAU2_GREETING},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{"name": tool, "arguments": {}}]},
        {"role": "tool", "content": "x" * 10, "error": False},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "###STOP###"},
    )
    return Episode(
        task_id=task_id,
        rep=rep,
        temperature=0.0,
        reward=1.0,
        trajectory_file="f",
        messages=messages,
    )


def _card(**overrides):
    kwargs = dict(
        episodes=[_episode("1", 0, "a"), _episode("1", 1, "a"), _episode("2", 0, "b")],
        train_ids=["1", "2", "3"],
        logged_cells=9,
        tool_schemas=[_schema("a"), _schema("b"), _schema("c")],
        system_prompt="p" * 35,
        catalog_sizes=[2, 5],
    )
    kwargs.update(overrides)
    episodes = kwargs.pop("episodes")
    return build_card(episodes, **kwargs)


def test_decisions_exclude_the_greeting_and_split_calls_from_replies():
    dec = _card()["decisions"]
    assert (dec["total"], dec["tool_calls"], dec["replies"]) == (6, 3, 3)


def test_task_coverage_names_the_tasks_training_will_never_see():
    ep = _card()["episodes"]
    assert ep["tasks_with_success"] == 2
    assert ep["tasks_without_success"] == ["3"]
    assert ep["episodes_per_task"] == {2: 1, 1: 1}


def test_tool_use_counts_every_native_tool_including_the_unused():
    tools = _card()["tools"]
    assert tools["use"] == {"a": 2, "b": 1, "c": 0}
    assert tools["never_called"] == ["c"]
    assert tools["unknown_called"] == []


def test_a_catalog_smaller_than_the_native_set_is_flagged_not_estimated():
    card = _card()
    assert card["tools"]["catalogs_smaller_than_native"] == [2]
    assert set(card["prompt_lengths"]["variants"]) == {
        "decision_catalog3",
        "decision_catalog3_no_policy",
        "episode_catalog3",
        "decision_catalog5_extrapolated",
    }


def test_lengths_come_from_the_real_serializer_and_the_given_counter():
    """Counting characters exactly makes the arithmetic checkable."""
    card = _card(count_tokens=len)
    pl = card["prompt_lengths"]
    variants = pl["variants"]
    assert pl["system_prompt_tokens"] == 35
    assert (
        variants["decision_catalog3"]["max"] - variants["decision_catalog3_no_policy"]["max"] == 35
    )
    extra = variants["decision_catalog5_extrapolated"]["max"] - variants["decision_catalog3"]["max"]
    assert extra == round(pl["native_tools_tokens"] / 3 * 2)
    # a whole episode is longer than any single decision prompt inside it
    assert variants["episode_catalog3"]["max"] > variants["decision_catalog3"]["max"]


def test_percentile_is_nearest_rank():
    assert pct([1, 2, 3, 4], 50) == 2.0
    assert pct([1, 2, 3, 4], 95) == 4.0
    assert pct([], 50) == 0.0


def test_markdown_renders_the_flag_for_an_undersized_catalog():
    md = render_markdown(_card(), header={"Teacher": "t"})
    assert "smaller than the 3 native tools" in md
