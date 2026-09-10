"""Rate limiting for a provider that meters requests, not tokens.

Week 0 established the hard numbers: OpenRouter allows 20 requests/minute and
1,000/day, both ACCOUNT-WIDE across every :free model, so the agent and the user
simulator draw on one shared budget.

tau2-bench has no pacing of its own. Left alone it fires flat out, gets a 429,
retries ~2s later while the API asks for 15-25s, and each retry digs the hole
deeper — a self-inflicted cascade we watched happen three times on 2026-09-09.
So the limiter goes in front of every call tau2 makes, by wrapping
litellm.completion in-process. No proxy server, nothing to keep running.

The daily counter is persisted, because the cap is per calendar day and the
harvest spans six of them. Losing the count on restart would either overshoot
the cap or waste an evening.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_STATE = Path("results/.quota.json")

# Sit just under the ceiling. At exactly 20/min a burst that lands on a window
# boundary still trips, and one 429 is worth more than one skipped request.
RPM_SAFETY = 0.9


class DailyQuotaExhausted(RuntimeError):
    """Today's request budget is spent. Stop cleanly and resume tomorrow."""


def _utc_today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class RateLimiter:
    """Blocks until a request is allowed. Thread-safe; tau2 runs concurrently."""

    def __init__(
        self,
        requests_per_minute: int,
        requests_per_day: int | None,
        state_path: str | Path = DEFAULT_STATE,
        safety: float = RPM_SAFETY,
    ):
        self.rpm = max(1, int(requests_per_minute * safety))
        self.rpd = requests_per_day
        self.state_path = Path(state_path)
        self._recent: deque[float] = deque()
        self._lock = threading.Lock()
        self._day, self._used = self._load()

    # --- persistence --------------------------------------------------------

    def _load(self) -> tuple[str, int]:
        today = _utc_today()
        if not self.state_path.exists():
            return today, 0
        try:
            data = json.loads(self.state_path.read_text())
        except (json.JSONDecodeError, OSError):
            return today, 0
        if data.get("day") != today:
            return today, 0  # new UTC day, counter resets
        return today, int(data.get("used", 0))

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"day": self._day, "used": self._used}))

    # --- accounting ---------------------------------------------------------

    @property
    def used_today(self) -> int:
        with self._lock:
            self._roll_day()
            return self._used

    @property
    def remaining_today(self) -> int | None:
        if self.rpd is None:
            return None
        return max(0, self.rpd - self.used_today)

    def _roll_day(self) -> None:
        today = _utc_today()
        if today != self._day:
            self._day, self._used = today, 0

    # --- the gate -----------------------------------------------------------

    def acquire(self) -> None:
        """Block until one request may be sent. Raise when the day is spent."""
        while True:
            with self._lock:
                self._roll_day()
                if self.rpd is not None and self._used >= self.rpd:
                    raise DailyQuotaExhausted(
                        f"{self._used}/{self.rpd} requests used on {self._day} (UTC). "
                        "Resume tomorrow — completed work is already logged and will be skipped."
                    )

                now = time.monotonic()
                while self._recent and now - self._recent[0] >= 60.0:
                    self._recent.popleft()

                if len(self._recent) < self.rpm:
                    self._recent.append(now)
                    self._used += 1
                    self._save()
                    return

                wait = 60.0 - (now - self._recent[0]) + 0.05
            time.sleep(max(wait, 0.01))  # outside the lock, so others can proceed


def install(limiter: RateLimiter) -> None:
    """Route every litellm.completion call through the limiter.

    Wrapping the library rather than running a proxy keeps this to one process
    and means anything tau2 does internally is covered, including calls we did
    not anticipate.
    """
    import litellm

    if getattr(litellm.completion, "_throttled", False):
        return

    inner = litellm.completion

    def throttled(*args, **kwargs):
        limiter.acquire()
        return inner(*args, **kwargs)

    throttled._throttled = True  # type: ignore[attr-defined]
    litellm.completion = throttled
