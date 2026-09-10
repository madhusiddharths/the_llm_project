"""Failure classification into the five buckets.

Week 3. Every wrong step lands in exactly one bucket (plan §3):

  1. hallucinated_tool      name not in catalog
  2. wrong_tool_neighbor    plausible near-neighbor
  3. wrong_tool_unrelated
  4. right_tool_wrong_args
  5. premature_termination  replied to the user instead of calling a tool

Buckets 1 and 4 have different fixes, which is why they are separate. Acceptance
test: agrees with your 30 hand-labels at >= 90%.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Support the invocation documented in division-of-labor.md — `python src/x.py
# --smoke` — which puts src/ on sys.path rather than the repo root. `python -m
# src.x` works either way.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cli import stub_main

BUCKETS = (
    "hallucinated_tool",
    "wrong_tool_neighbor",
    "wrong_tool_unrelated",
    "right_tool_wrong_args",
    "premature_termination",
)

if __name__ == "__main__":
    raise SystemExit(stub_main("taxonomy", "Failure taxonomy classifier", "plan §6, week 3"))
