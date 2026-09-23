"""A failed episode must not be recorded as a finished one.

On 2026-09-10 the harvest logged tau2's INFRASTRUCTURE_ERROR simulations with
reward None. RunLog.completed_cells() keys on (task_id, seed) and does not look
at reward, so those cells counted as done and the next run skipped them — the
harvest would have finished 18 tasks short without printing an error.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.harvest import (
    MAX_REQUESTS_PER_EPISODE,
    affordable_episodes,
    batch_path,
    episode_reward,
)
from src.throttle import RateLimiter


def _limiter(tmp_path, rpd, already_used=0):
    lim = RateLimiter(600, rpd, state_path=tmp_path / "q.json", safety=1.0)
    for _ in range(already_used):
        lim.acquire()
    return lim


def _sim(reward):
    return SimpleNamespace(reward_info=SimpleNamespace(reward=reward))


def test_a_scored_episode_reports_its_reward():
    assert episode_reward(_sim(1.0)) == 1.0


def test_a_scored_failure_is_zero_not_none():
    """The distinction the resume logic depends on: 0.0 is a result, None is not."""
    assert episode_reward(_sim(0.0)) == 0.0


def test_an_episode_that_never_ran_reports_none():
    """What tau2 hands back after exhausting its retries."""
    failed = SimpleNamespace(reward_info=None, messages=[], termination_reason=None)
    assert episode_reward(failed) is None


def test_a_simulation_without_reward_info_at_all_reports_none():
    assert episode_reward(SimpleNamespace()) is None


# --- never start an episode the day's budget cannot finish -------------------
#
# tau2 runs each task inside `except Exception`, so DailyQuotaExhausted raised by
# the limiter never reaches main()'s handler — tau2 reads it as a task failure and
# moves on. On 2026-09-11 that marched through all 25 remaining batches, and left
# 73 requests (7% of the day) inside episodes abandoned mid-flight.


def test_a_full_batch_is_allowed_when_the_budget_is_ample(tmp_path):
    assert affordable_episodes(_limiter(tmp_path, rpd=1000), 4) == 4


def test_the_batch_shrinks_to_what_the_budget_can_fund(tmp_path):
    """Better to run 2 episodes to completion than start 4 and abandon the last."""
    lim = _limiter(tmp_path, rpd=2 * MAX_REQUESTS_PER_EPISODE)
    assert affordable_episodes(lim, 4) == 2


def test_budget_for_less_than_one_episode_funds_nothing(tmp_path):
    """Zero is the signal main() turns into a clean stop, not a partial episode."""
    lim = _limiter(tmp_path, rpd=MAX_REQUESTS_PER_EPISODE - 1)
    assert affordable_episodes(lim, 4) == 0


def test_an_exhausted_budget_funds_nothing(tmp_path):
    lim = _limiter(tmp_path, rpd=50, already_used=50)
    assert affordable_episodes(lim, 4) == 0


def test_no_daily_cap_means_no_ceiling(tmp_path):
    """requests_per_day is Optional in TeacherConfig; a provider without a daily
    cap must not be throttled to zero by this guard."""
    assert affordable_episodes(_limiter(tmp_path, rpd=None), 4) == 4


def test_the_guard_budgets_above_the_measured_mean():
    """An episode killed at 90% has spent everything it spent, so the guard has to
    use the worst case (22 measured) not the mean (15.7)."""
    assert MAX_REQUESTS_PER_EPISODE >= 22


# --- batch files never collide across sessions --------------------------------
#
# 2026-09-13: day 2's rep1-batch000 collided with day 1's, and tau2 stopped on a
# y/n resume prompt where both answers raise.


def test_the_same_batch_index_on_two_days_gets_two_files(tmp_path):
    day1 = batch_path(tmp_path, "20260912T000400Z", rep=1, index=0)
    day2 = batch_path(tmp_path, "20260913T000100Z", rep=1, index=0)
    assert day1 != day2


def test_reps_do_not_share_files_within_a_session(tmp_path):
    session = "20260913T000100Z"
    assert batch_path(tmp_path, session, 1, 0) != batch_path(tmp_path, session, 2, 0)


def test_batch_files_sort_in_the_order_they_were_written(tmp_path):
    names = [
        batch_path(tmp_path, "20260913T000100Z", 1, 0).name,
        batch_path(tmp_path, "20260912T000400Z", 1, 3).name,
        batch_path(tmp_path, "20260912T000400Z", 1, 0).name,
    ]
    assert sorted(names) == [names[2], names[1], names[0]]


# --- padded catalogs (D7) ------------------------------------------------------

from src.harvest import MAX_CONSECUTIVE_EMPTY_BATCHES, order_agent_tools  # noqa: E402


def _schema(name):
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def test_the_agent_sees_the_catalog_order_with_real_native_tools():
    env = {"a": "TOOL_A", "b": "TOOL_B"}
    catalog = [_schema("x"), _schema("b"), _schema("a"), _schema("y")]
    ordered = order_agent_tools(env, catalog, lambda t: f"stub:{t['function']['name']}")
    assert ordered == ["stub:x", "TOOL_B", "TOOL_A", "stub:y"]


def test_a_catalog_missing_a_native_tool_is_refused():
    import pytest

    with pytest.raises(ValueError, match="missing native tools"):
        order_agent_tools({"a": 1, "b": 2}, [_schema("a")], lambda t: t)


def test_the_empty_batch_breaker_trips_before_a_days_quota_is_gone():
    """2026-09-16 burned 981 requests on failures; the breaker caps that at
    two batches of four episodes, worst case ~180 requests."""
    assert MAX_CONSECUTIVE_EMPTY_BATCHES * 4 * MAX_REQUESTS_PER_EPISODE < 200
