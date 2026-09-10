"""The limiter must never exceed the ceilings that broke us on 2026-09-09."""

from __future__ import annotations

import json
import threading
import time

import pytest

from src.throttle import DailyQuotaExhausted, RateLimiter


def _limiter(tmp_path, rpm=600, rpd=None, safety=1.0):
    return RateLimiter(rpm, rpd, state_path=tmp_path / "q.json", safety=safety)


# --- daily cap ---------------------------------------------------------------


def test_daily_cap_stops_cleanly(tmp_path):
    lim = _limiter(tmp_path, rpd=3)
    for _ in range(3):
        lim.acquire()
    with pytest.raises(DailyQuotaExhausted, match="3/3"):
        lim.acquire()


def test_remaining_counts_down(tmp_path):
    lim = _limiter(tmp_path, rpd=5)
    assert lim.remaining_today == 5
    lim.acquire()
    lim.acquire()
    assert lim.remaining_today == 3


def test_count_survives_a_restart(tmp_path):
    """The harvest spans six days; losing the count overshoots the cap."""
    first = _limiter(tmp_path, rpd=10)
    for _ in range(4):
        first.acquire()
    second = _limiter(tmp_path, rpd=10)
    assert second.used_today == 4
    assert second.remaining_today == 6


def test_no_daily_cap_never_raises(tmp_path):
    lim = _limiter(tmp_path, rpd=None)
    for _ in range(50):
        lim.acquire()
    assert lim.remaining_today is None


def test_corrupt_state_file_does_not_block_a_run(tmp_path):
    (tmp_path / "q.json").write_text("{not json")
    lim = _limiter(tmp_path, rpd=5)
    lim.acquire()
    assert lim.used_today == 1


def test_state_from_another_day_resets(tmp_path):
    (tmp_path / "q.json").write_text(json.dumps({"day": "2020-01-01", "used": 999}))
    lim = _limiter(tmp_path, rpd=10)
    assert lim.used_today == 0


# --- per-minute ceiling ------------------------------------------------------


def test_rpm_ceiling_is_enforced(tmp_path):
    """Third call must block once two are already inside the 60s window."""
    lim = _limiter(tmp_path, rpm=2, safety=1.0)
    lim.acquire()
    lim.acquire()

    done = threading.Event()
    threading.Thread(target=lambda: (lim.acquire(), done.set()), daemon=True).start()
    assert not done.wait(timeout=0.5), "third call should have been held back"


def test_safety_margin_shaves_the_ceiling(tmp_path):
    """20 RPM at 0.9 becomes 18 — one 429 costs more than one skipped slot."""
    assert _limiter(tmp_path, rpm=20, safety=0.9).rpm == 18


def test_rpm_never_rounds_to_zero(tmp_path):
    assert _limiter(tmp_path, rpm=1, safety=0.1).rpm == 1


# --- concurrency (tau2 runs episodes in parallel) ----------------------------


def test_concurrent_callers_never_exceed_the_daily_cap(tmp_path):
    lim = _limiter(tmp_path, rpd=20)
    granted = []
    lock = threading.Lock()

    def worker():
        for _ in range(10):
            try:
                lim.acquire()
            except DailyQuotaExhausted:
                return
            with lock:
                granted.append(1)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(granted) == 20, f"granted {len(granted)}, cap was 20"
    assert lim.used_today == 20


def test_install_is_idempotent(tmp_path):
    litellm = pytest.importorskip("litellm")  # absent in CI's dev-only deps

    from src.throttle import install

    original = litellm.completion
    try:
        lim = _limiter(tmp_path)
        install(lim)
        once = litellm.completion
        install(lim)
        assert litellm.completion is once, "double-wrapping would double-count"
    finally:
        litellm.completion = original


def test_installed_wrapper_consumes_quota(tmp_path):
    litellm = pytest.importorskip("litellm")

    from src.throttle import install

    original = litellm.completion
    try:
        litellm.completion = lambda **kw: "ok"
        lim = _limiter(tmp_path, rpd=2)
        install(lim)
        assert litellm.completion(model="x") == "ok"
        assert lim.used_today == 1
    finally:
        litellm.completion = original


def test_acquire_is_fast_when_under_the_ceiling(tmp_path):
    lim = _limiter(tmp_path, rpm=600)
    start = time.monotonic()
    for _ in range(50):
        lim.acquire()
    assert time.monotonic() - start < 1.0
