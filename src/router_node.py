"""LangGraph router node with logprob-thresholded escalation.

Week 4. Calls the served adapter over HTTP, extracts mean token logprob over the
tool-name span, and escalates to the teacher below threshold. The logprob
extraction is a SHARED component — division-of-labor.md wants you reading it
line by line, specifically that it averages over the tool-name span and not the
whole completion.
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
    raise SystemExit(stub_main("router_node", "Router node with escalation", "plan §6, week 4"))
