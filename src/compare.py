"""Paired forced-eval comparison of two adapters, per metric and per failure bucket
(plan G3-2; the same table E4-2 would have used for DPO).

`report.py` puts adapters side by side with an interval for each one alone.
A method comparison (GRPO vs its SFT parent) needs the interval on the
DIFFERENCE: both replay the same episodes and steps, so the episodes are paired
and most of the per-episode noise cancels. The unit is the episode, as in
report.py: a paired bootstrap resamples episodes (with replacement) and
recomputes each rate as a ratio of sums over the resampled episodes, for both
adapters at once.

Rates:
  step_agreement   call steps, normalized match
  tool_name        call steps, right tool name(s)
  reply_as_reply   reply steps answered with a reply
  call_as_call     call steps answered with a tool call
  <bucket>         src/taxonomy.py buckets: the five over call steps,
                   call_instead_of_reply over reply steps, malformed over all

    python src/compare.py --catalog 16 --base qwen3b-sft --new qwen3b-grpo-mlx
    python src/compare.py --grpo --markdown     # every size x catalog with a GRPO file
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.report import load_forced
from src.taxonomy import BUCKETS, classify

RESAMPLES = 2000
SEED = 0
SIZES = ("qwen05b", "qwen15b", "qwen3b")
CATALOGS = (16, 40, 80)

Step = Mapping[str, Any]
CALL = "tool_call"


def _is_call(s: Step) -> bool:
    return s["reference_kind"] == CALL


def _is_reply(s: Step) -> bool:
    return s["reference_kind"] == "reply"


def _rates() -> dict[str, tuple[Callable[[Step], bool], Callable[[Step], bool]]]:
    """name -> (which steps count, which of them are hits)."""
    rates: dict[str, tuple[Callable[[Step], bool], Callable[[Step], bool]]] = {
        "step_agreement": (_is_call, lambda s: bool(s["normalized_match"])),
        "tool_name": (_is_call, lambda s: bool(s["name_match"])),
        "reply_as_reply": (_is_reply, lambda s: bool(s["kind_match"])),
        "call_as_call": (_is_call, lambda s: s["predicted_kind"] == CALL),
    }
    for b in BUCKETS:
        rates[b] = (_is_call, lambda s, b=b: classify(s) == b)
    rates["call_instead_of_reply"] = (_is_reply, lambda s: classify(s) == "call_instead_of_reply")
    rates["malformed_output"] = (lambda s: True, lambda s: classify(s) == "malformed_output")
    return rates


RATES = _rates()


def pair_episodes(
    base: Sequence[Mapping[str, Any]], new: Sequence[Mapping[str, Any]]
) -> list[tuple[Sequence[Step], Sequence[Step]]]:
    """Episodes matched by (task_id, seed). Both sides must have replayed the
    same episodes and the same steps, or the comparison is not paired."""

    def key(r: Mapping[str, Any]) -> tuple[str, int]:
        return (str(r["task_id"]), int(r["seed"]))

    a, b = {key(r): r for r in base}, {key(r): r for r in new}
    if a.keys() != b.keys():
        raise ValueError(
            f"episodes differ: {len(a.keys() - b.keys())} only in base, "
            f"{len(b.keys() - a.keys())} only in new"
        )
    pairs = []
    for k in sorted(a):
        sa, sb = a[k]["steps"], b[k]["steps"]
        if [s["step_id"] for s in sa] != [s["step_id"] for s in sb]:
            raise ValueError(f"episode {k}: step ids differ between base and new")
        pairs.append((sa, sb))
    return pairs


def compare(
    pairs: Sequence[tuple[Sequence[Step], Sequence[Step]]],
    *,
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> dict[str, Any]:
    """Each rate for base and new, and the paired-bootstrap 95% CI of new - base."""
    rng = random.Random(seed)
    draws = [[rng.randrange(len(pairs)) for _ in pairs] for _ in range(resamples)]
    out: dict[str, Any] = {}
    for name, (counts, hit) in RATES.items():
        # per episode: (denominator, base hits, new hits); the denominator is
        # shared because the steps are the same on both sides
        ep = [
            (
                sum(counts(s) for s in sa),
                sum(counts(s) and hit(s) for s in sa),
                sum(counts(s) and hit(s) for s in sb),
            )
            for sa, sb in pairs
        ]
        den = sum(e[0] for e in ep)
        if not den:
            out[name] = None
            continue
        a, b = sum(e[1] for e in ep) / den, sum(e[2] for e in ep) / den
        deltas = []
        for idx in draws:
            d = sum(ep[i][0] for i in idx)
            if d:
                deltas.append((sum(ep[i][2] for i in idx) - sum(ep[i][1] for i in idx)) / d)
        deltas.sort()
        out[name] = {
            "n_steps": den,
            "base": round(a, 4),
            "new": round(b, 4),
            "delta": round(b - a, 4),
            "ci95_low": round(deltas[int(0.025 * len(deltas))], 4),
            "ci95_high": round(deltas[int(0.975 * len(deltas)) - 1], 4),
        }
    return out


def run_pair(
    by_label: Mapping[str, Sequence[Mapping[str, Any]]], catalog: int, base: str, new: str
) -> dict[str, Any]:
    """`by_label` is report.load_forced's output for `catalog`."""
    missing = [x for x in (base, new) if x not in by_label]
    if missing:
        raise FileNotFoundError(f"c{catalog}: no forced results for {missing}")
    pairs = pair_episodes(by_label[base], by_label[new])
    return {
        "catalog": catalog,
        "base": base,
        "new": new,
        "n_episodes": len(pairs),
        "rates": compare(pairs),
    }


def grpo_pairs(by_catalog: Mapping[int, Mapping[str, Any]]) -> list[tuple[int, str, str]]:
    """(catalog, base, new) for every GRPO forced result, from load_forced's
    labels per catalog. The SFT base is the MLX run when one exists at that
    catalog (same engine), else the vLLM run (the MLX calibration matched it
    within 0.1 pt)."""
    out = []
    for size in SIZES:
        for c in CATALOGS:
            labels = by_catalog.get(c, {})
            new = f"{size}-grpo-mlx"
            if new in labels:
                mlx = f"{size}-sft-mlx"
                out.append((c, mlx if mlx in labels else f"{size}-sft", new))
    return out


def _pp(x: float) -> str:
    return f"{100 * x:+.1f}"


def markdown(result: Mapping[str, Any]) -> str:
    lines = [
        f"**{result['new']} vs {result['base']}, c{result['catalog']}** "
        f"({result['n_episodes']} episodes, paired bootstrap 95% CI)",
        "",
        "| Rate | Steps | Base | New | Delta (pts) | 95% CI |",
        "|---|---|---|---|---|---|",
    ]
    for name, r in result["rates"].items():
        if r is None:
            continue
        sig = "" if r["ci95_low"] <= 0 <= r["ci95_high"] else " *"
        lines.append(
            f"| {name} | {r['n_steps']} | {100 * r['base']:.1f}% | {100 * r['new']:.1f}% "
            f"| {_pp(r['delta'])}{sig} | [{_pp(r['ci95_low'])}, {_pp(r['ci95_high'])}] |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Paired forced-eval comparison (G3-2)")
    p.add_argument("--results-dir", type=Path, default=Path("results"))
    p.add_argument("--catalog", type=int)
    p.add_argument("--base", help="label, e.g. qwen3b-sft")
    p.add_argument("--new", help="label, e.g. qwen3b-grpo-mlx")
    p.add_argument("--grpo", action="store_true", help="every GRPO file vs its SFT parent")
    p.add_argument("--markdown", action="store_true")
    args = p.parse_args(argv)

    if args.grpo:
        catalogs = list(CATALOGS)
    elif args.catalog and args.base and args.new:
        catalogs = [args.catalog]
    else:
        p.error("give --grpo, or --catalog with --base and --new")
    by_catalog = {c: load_forced(args.results_dir, c) for c in catalogs}  # each file read once
    jobs = grpo_pairs(by_catalog) if args.grpo else [(args.catalog, args.base, args.new)]

    results = [run_pair(by_catalog[c], c, b, n) for c, b, n in jobs]
    for r in results:
        print(markdown(r) + "\n" if args.markdown else json.dumps(r, indent=2))
    if args.grpo:
        out = args.results_dir / "compare-grpo-vs-sft.json"
        out.write_text(json.dumps(results, indent=2) + "\n")
        print(f"[compare] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
