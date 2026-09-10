"""LoRA fine-tuning, one config per YAML.

Week 2. Identical data and hyperparameters across rungs; only the base model
changes. Must checkpoint to the HF Hub every epoch and resume from the last
checkpoint on restart — Kaggle sessions die at 12 hours (plan §7).

Runs on Kaggle, never on the Mac: one canonical training path (plan §7).
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
    raise SystemExit(stub_main("train", "LoRA fine-tuning", "plan §6, week 2"))
