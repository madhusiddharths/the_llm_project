"""Free-running end-to-end evaluation over tau2-bench (retail).

Week 3. The student drives and its own mistakes propagate; the harness scores
the final DB state. Expensive and high-variance, so 5 seeds minimum with CIs.

Harness note (decided 2026-09-09): the original tau-bench is frozen upstream
with known task bugs and its README redirects to the successor, so this project
uses tau2-bench's retail domain. It is cloned as a SIBLING directory, not
vendored here, and reached through an adapter that maps its env to our own
Trajectory/ToolCatalog types — so a switch to the BFCL fallback (the Gate 1
contingency) touches one module, not this one.

Must skip completed (config, task, seed) cells on restart — see src/runlog.py.
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
        stub_main("eval_free", "Free-running end-to-end evaluation", "plan §6, week 3")
    )
