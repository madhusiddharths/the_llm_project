"""Forced-eval comparison tables and figures (plan V1-14, V1-20).

`eval_forced.py` scores one completions file at a time and prints that file's
numbers. Nothing until now put those files side by side, which is what a gate
call needs: zero-shot against fine-tuned, at one catalog, with a confidence
interval wide enough to say whether the gap is real.

The unit for the interval is the EPISODE, not the step. Steps inside one
replayed episode share a task, a simulator and a conversation prefix, so they
are nowhere near independent; a CI over 1,416 steps would be about three times
too narrow. Each episode contributes one number — its own step agreement over
its tool-call steps — and the interval is taken across episodes.

Reads only `results/forced-*-c<N>.jsonl`, so it runs on the Mac in seconds and
never needs the GPU half.

    python src/report.py --catalog 16                    # table to stdout
    python src/report.py --catalog 16 --markdown         # README-ready
    python src/report.py --catalog 16 --figure results/forced-c16.png
    python src/report.py --catalog 16 --gate2 qwen15b    # the Gate 2 call
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.metrics import summarize
from src.runlog import read_records
from src.stats import mean_ci

# V1-11: the fine-tuned 1.5B must beat zero-shot by this many points of step
# agreement for Gate 2 to pass.
GATE2_MARGIN = 0.15


def _label(config_name: str) -> str:
    """ "forced:qwen15b-sft:c16" -> "qwen15b-sft"."""
    parts = config_name.split(":")
    return parts[1] if len(parts) >= 2 else config_name


def load_forced(
    results_dir: Path, catalog: int, *, smoke: bool = False
) -> dict[str, list[dict[str, Any]]]:
    """Every scored forced-eval file at this catalog, keyed by label.

    A file may hold records from more than one scoring identity (METRICS_VERSION
    moved, the catalog was rebuilt, episodes were excluded). The LAST identity
    in file order wins, because RunLog appends and a re-score is therefore
    always newer than what it replaces. Picking the most numerous one instead
    would keep a superseded scoring run whenever it covered more episodes —
    which is exactly what excluding the degenerate episodes does.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    suffix = ".smoke" if smoke else ""
    for path in sorted(results_dir.glob(f"forced-*-c{catalog}{suffix}.jsonl")):
        if not smoke and path.name.endswith(".smoke.jsonl"):
            continue
        records = [r for r in read_records(path) if bool(r.get("smoke")) == smoke]
        if not records:
            continue
        counts = Counter(r["config_fingerprint"] for r in records)
        keep = records[-1]["config_fingerprint"]
        if len(counts) > 1:
            dropped = sum(n for fp, n in counts.items() if fp != keep)
            print(f"[report] {path.name}: {len(counts)} identities, ignoring {dropped} superseded")
        kept = [r for r in records if r["config_fingerprint"] == keep]
        out[_label(kept[0]["config_name"])] = kept
    return out


def per_episode_agreement(records: list[dict[str, Any]]) -> list[float]:
    """One step-agreement figure per episode, over its tool-call steps.

    Episodes whose steps are all replies contribute nothing: step agreement is
    undefined there, and counting them as 0 or 1 would move the mean.
    """
    values = []
    for rec in records:
        calls = [s for s in rec["steps"] if s["reference_kind"] == "tool_call"]
        if calls:
            values.append(sum(bool(s["normalized_match"]) for s in calls) / len(calls))
    return values


def forced_table(results_dir: Path, catalog: int, *, smoke: bool = False) -> dict[str, Any]:
    by_label = load_forced(results_dir, catalog, smoke=smoke)
    rows: dict[str, Any] = {}
    for label, records in sorted(by_label.items()):
        steps = [s for rec in records for s in rec["steps"]]
        episodes = per_episode_agreement(records)
        rows[label] = {
            "n_episodes": len(records),
            **summarize(steps),
            "step_agreement_by_episode": mean_ci(episodes) if len(episodes) > 1 else None,
        }
    return {"catalog": catalog, "smoke": smoke, "labels": rows}


def gate2(table: dict[str, Any], size: str) -> dict[str, Any]:
    """Zero-shot against fine-tuned at one size. V1-11 wants +15 points."""
    labels = table["labels"]
    zero, sft = f"{size}-zeroshot", f"{size}-sft"
    missing = [k for k in (zero, sft) if k not in labels]
    if missing:
        return {"size": size, "verdict": "incomplete", "missing": missing}
    a, b = labels[zero]["step_agreement"], labels[sft]["step_agreement"]
    delta = round(b - a, 4)
    return {
        "size": size,
        "catalog": table["catalog"],
        "zeroshot": a,
        "sft": b,
        "delta": delta,
        "margin": GATE2_MARGIN,
        "verdict": "pass" if delta >= GATE2_MARGIN else "fail",
        "zeroshot_ci": labels[zero]["step_agreement_by_episode"],
        "sft_ci": labels[sft]["step_agreement_by_episode"],
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def markdown_table(table: dict[str, Any]) -> str:
    lines = [
        f"| Model (catalog {table['catalog']}) | Episodes | Steps | Step agreement "
        "| 95% CI (by episode) | Tool name | Decision type | Parse errors |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for label, row in table["labels"].items():
        ci = row["step_agreement_by_episode"]
        span = f"{_pct(ci['ci95_low'])} to {_pct(ci['ci95_high'])}" if ci else "n/a"
        lines.append(
            f"| {label} | {row['n_episodes']} | {row['n_steps']} "
            f"| {_pct(row['step_agreement'])} | {span} "
            f"| {_pct(row['tool_name_accuracy'])} | {_pct(row['decision_type_accuracy'])} "
            f"| {_pct(row['parse_error_rate'])} |"
        )
    return "\n".join(lines)


def figure(table: dict[str, Any], out_path: Path) -> Path:
    """Step agreement per label with episode-level 95% intervals."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    items = [
        (label, row["step_agreement_by_episode"])
        for label, row in table["labels"].items()
        if row["step_agreement_by_episode"]
    ]
    if not items:
        raise ValueError("nothing to plot: no label has a tool-call step")
    names = [k for k, _ in items]
    means = [v["mean"] for _, v in items]
    errs = [v["mean"] - v["ci95_low"] for _, v in items]

    fig, ax = plt.subplots(figsize=(1.6 * len(names) + 2, 4))
    ax.bar(names, means, yerr=errs, capsize=4, color="#4c72b0")
    ax.set_ylim(0, 1)
    ax.set_ylabel("step agreement")
    ax.set_title(f"Teacher-forced step agreement, catalog {table['catalog']}")
    ax.tick_params(axis="x", rotation=20)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Forced-eval comparison table")
    parser.add_argument("--config", default="configs/qwen05b.yaml")
    parser.add_argument("--catalog", type=int, default=16)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--markdown", action="store_true", help="README-ready table")
    parser.add_argument("--figure", type=Path, help="write a bar chart here")
    parser.add_argument("--gate2", metavar="SIZE", help="e.g. qwen15b: the Gate 2 comparison")
    parser.add_argument("--out", type=Path, help="write the table as JSON")
    args = parser.parse_args(argv)

    from src.config import load_config

    cfg = load_config(args.config, smoke=args.smoke)
    table = forced_table(cfg.paths.results_dir, args.catalog, smoke=args.smoke)
    if not table["labels"]:
        print(f"[report] no forced-*-c{args.catalog} results yet")
        return 1

    if args.markdown:
        print(markdown_table(table))
    else:
        print(json.dumps(table, indent=2))
    if args.gate2:
        print("\n[report] Gate 2:", json.dumps(gate2(table, args.gate2), indent=2))
    if args.figure:
        print(f"[report] wrote {figure(table, args.figure)}")
    if args.out:
        args.out.write_text(json.dumps(table, indent=2) + "\n")
        print(f"[report] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
