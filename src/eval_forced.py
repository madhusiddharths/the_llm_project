"""Teacher-forced replay: the per-step number.

Week 1. Replay a logged teacher trajectory; at each state ask the student what
it would do and compare to the teacher's action. Then advance the state using
the TEACHER's action regardless of what the student said — that detail is the
whole method, and getting it wrong silently turns this into free-running eval.

Deterministic, cheap, no user simulator, ~2 minutes per config. Cheap enough
that there is no excuse for training blind.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Support the invocation documented in division-of-labor.md — `python src/x.py
# --smoke` — which puts src/ on sys.path rather than the repo root. `python -m
# src.x` works either way.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cli import stub_main

if __name__ == "__main__":
    raise SystemExit(
        stub_main("eval_forced", "Teacher-forced per-step evaluation", "plan §6, week 1")
    )
