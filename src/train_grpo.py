"""Stage G step-level GRPO (plan X7). Body lands with G2-1, after reward.py (G1-1).

A stub (V2-01): --smoke exercises the shared plumbing (config, invariants, the
run log) and exits 0; without --smoke it refuses, so it can never be mistaken
for a working pipeline. See cli.stub_main.

    python src/train_grpo.py --config configs/qwen05b.yaml --smoke
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cli import stub_main


def main(argv: list[str] | None = None) -> int:
    return stub_main(
        "train_grpo", "Stage G step-level GRPO (plan X7)", "G2-1 (execution-plan.md)", argv
    )


if __name__ == "__main__":
    raise SystemExit(main())
