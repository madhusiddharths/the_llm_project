"""Teacher -> (state, action) pairs.

Week 1. Runs the teacher over the training split at three temperatures, applies
rejection sampling (only successful trajectories become training data), caches
to disk immediately, and backs off on the free tier's daily cap. API-bound and
quota-free, so this is the overnight job the plan wants you sleeping through.

Heavy imports belong inside the functions below, never at module scope — see
tests/test_core_is_light.py.
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
    raise SystemExit(stub_main("harvest", "Harvest teacher trajectories", "plan §6, week 1"))
