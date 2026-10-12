"""Is the MLX student the vLLM student? (lane 2 calibration, 2026-10-11)

Compares two completions files for the same prompts, step by step: the vLLM
run from Kaggle and the MLX run on the Mac. The bar for moving inference to the
Mac: at least 95% of completions identical (greedy decoding, same weights), and
the forced-eval step agreement within 1 point (eval_forced on the MLX file,
then report.py shows it next to the vLLM row).

Spot checks on 2026-10-11: 30/30 identical (0.5B zero-shot, c16), 25/25 (1.5B
SFT, c16), 12/12 (1.5B SFT, c80; log-probs within 0.03, p(<tool_call>) within
0.012).

    python src/engine_check.py results/completions-qwen15b-sft-c16.jsonl \\
        results/completions-qwen15b-sft-mlx-c16.jsonl
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

IDENTICAL_BAR = 0.95


def compare(
    a: Mapping[str, Mapping[str, Any]], b: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """Steps present in both files; a completion matches if its text matches
    after stripping surrounding whitespace (vLLM and MLX may differ there only)."""
    common = sorted(set(a) & set(b))
    if not common:
        raise ValueError("the two files share no step_id")
    differ = [k for k in common if a[k]["completion"].strip() != b[k]["completion"].strip()]
    rate = 1 - len(differ) / len(common)
    return {
        "steps": len(common),
        "only_in_first": len(set(a) - set(b)),
        "only_in_second": len(set(b) - set(a)),
        "identical": len(common) - len(differ),
        "identical_rate": round(rate, 4),
        "passes": rate >= IDENTICAL_BAR,
        "differing_steps": differ,
    }


def main(argv: list[str] | None = None) -> int:
    from src.runlog import read_records

    parser = argparse.ArgumentParser(description="vLLM vs MLX completions, step by step")
    parser.add_argument("first", type=Path, help="e.g. the vLLM completions")
    parser.add_argument("second", type=Path, help="e.g. the MLX completions")
    parser.add_argument("--show", type=int, default=5, help="print this many differing steps")
    args = parser.parse_args(argv)

    a = {r["step_id"]: r for r in read_records(args.first)}
    b = {r["step_id"]: r for r in read_records(args.second)}
    out = compare(a, b)
    print(
        f"[engine_check] {out['identical']}/{out['steps']} identical "
        f"({out['identical_rate']:.1%}; bar {IDENTICAL_BAR:.0%}): "
        f"{'PASS' if out['passes'] else 'FAIL'}"
    )
    if out["only_in_first"] or out["only_in_second"]:
        print(
            f"[engine_check] not compared: {out['only_in_first']} steps only in the first "
            f"file, {out['only_in_second']} only in the second"
        )
    for k in out["differing_steps"][: args.show]:
        print(f"  {k}\n    1: {a[k]['completion'][:200]!r}\n    2: {b[k]['completion'][:200]!r}")
    return 0 if out["passes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
