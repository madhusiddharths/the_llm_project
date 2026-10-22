"""Stage F vLLM serving benchmarks (plan X6). Body lands with F1-2.

A stub (V2-01): --smoke exercises the shared plumbing (config, invariants, the
run log) and exits 0; without --smoke it refuses, so it can never be mistaken
for a working pipeline. See cli.stub_main.

    python src/serve_bench.py --config configs/qwen05b.yaml --smoke
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cli import stub_main


def main(argv: list[str] | None = None) -> int:
    return stub_main(
        "serve_bench", "Stage F vLLM serving benchmarks (plan X6)", "F1-2 (execution-plan.md)", argv
    )


if __name__ == "__main__":
    raise SystemExit(main())
