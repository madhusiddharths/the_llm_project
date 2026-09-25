"""Failure classification into the five buckets (plan §3, V1-14).

Every wrong TOOL-CALL step lands in exactly one bucket:

  1. hallucinated_tool      the name is not in the active catalog
  2. wrong_tool_neighbor    a real catalog tool that shares a content word with
                            the right one (cancel_order for cancel_pending_order)
  3. wrong_tool_unrelated   a real catalog tool that shares none
  4. right_tool_wrong_args  the right tool, arguments wrong after §3 normalization
  5. premature_termination  replied to the user instead of calling a tool

Buckets 1 and 4 have different fixes (constrained decoding vs arg-focused
data), which is why they are separate. Two outcomes fall outside the five and
are reported beside them, never folded in:

  malformed_output          the completion did not parse at all
  call_instead_of_reply     a REPLY step answered with a tool call (the five
                            buckets classify tool-call steps only, D3)

SHARED COMPONENT (division-of-labor.md): acceptance is >= 90% agreement with
30 hand labels. `--label-sheet` writes the sheet; `--check-labels` scores it.

"Neighbour" is judged against the reference tool, not by where the predicted
tool came from: two names are neighbours if they share a word after stemming
(src/catalogs.py name_words), ignoring connector words. get_order_details vs
get_product_details are neighbours (get, detail); get_order_details vs
calculate are not. A native tool can be the wrong neighbour of another native
tool, which is exactly the confusion bucket 2 exists to count.

    python src/taxonomy.py --results results/forced-qwen15b-c16.jsonl
    python src/taxonomy.py --results ... --label-sheet 30 --completions ... --prompts ...
    python src/taxonomy.py --check-labels results/label-sheet-qwen15b-c16.csv
"""

from __future__ import annotations

import csv
import json
import random
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.catalogs import name_words

BUCKETS = (
    "hallucinated_tool",
    "wrong_tool_neighbor",
    "wrong_tool_unrelated",
    "right_tool_wrong_args",
    "premature_termination",
)
OUTSIDE = ("malformed_output", "call_instead_of_reply")
CONNECTORS = {"by", "to", "id", "all", "the", "a", "an", "of", "for", "and", "with", "in", "on"}


def neighbours(a: str, b: str) -> bool:
    return bool((name_words(a) & name_words(b)) - CONNECTORS)


def classify(row: Mapping[str, Any]) -> str | None:
    """The bucket for one scored step (a metrics.StepScore dict), or None if the
    step agreed with the reference."""
    kind, predicted = row["reference_kind"], row["predicted_kind"]
    if kind == "reply":
        if row["kind_match"]:
            return None
        return "malformed_output" if predicted is None else "call_instead_of_reply"
    if row["normalized_match"]:
        return None
    if predicted is None:
        return "malformed_output"
    if predicted == "reply":
        return "premature_termination"
    if row["hallucinated_tool"]:
        return "hallucinated_tool"
    if row["name_match"]:
        return "right_tool_wrong_args"
    ref, pred = row.get("reference_tools") or (), row.get("predicted_tools") or ()
    if any(neighbours(p, r) for p in pred for r in ref):
        return "wrong_tool_neighbor"
    return "wrong_tool_unrelated"


def bucket_table(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Counts per bucket, and each of the five as a share of wrong tool-call steps."""
    labels = [classify(r) for r in rows]
    counts = Counter(label for label in labels if label is not None)
    wrong_calls = sum(counts[b] for b in BUCKETS) + counts["malformed_output"]
    return {
        "n_steps": len(labels),
        "n_wrong": sum(counts.values()),
        "counts": {b: counts[b] for b in (*BUCKETS, *OUTSIDE)},
        "share_of_wrong_tool_call_steps": {
            b: round(counts[b] / wrong_calls, 4) if wrong_calls else None for b in BUCKETS
        },
    }


def forced_rows(results_path: Path, fingerprint: str | None = None) -> list[dict[str, Any]]:
    """Step rows for ONE cell identity of a forced-eval RunLog.

    A log gains a new identity whenever what was scored changes (a new
    completions file, catalog, or METRICS_VERSION), and mixing identities would
    count the same steps twice. Without --fingerprint the most recently written
    identity is used, and the others are named so the choice is visible.
    """
    from src.runlog import read_records

    records = list(read_records(results_path))
    if not records:
        return []
    if fingerprint is None:
        fingerprint = str(records[-1]["config_fingerprint"])
        others = sorted({r["config_fingerprint"][:12] for r in records} - {fingerprint[:12]})
        if others:
            print(f"[taxonomy] using latest identity {fingerprint[:12]}; also in file: {others}")
    return [
        row
        for r in records
        if r["config_fingerprint"].startswith(fingerprint)
        for row in r["steps"]
    ]


# --- hand-label validation ---------------------------------------------------


def _tail(prompt: str, n: int = 2) -> str:
    """The last n turns before the decision, for a human to judge it."""
    turns = prompt.split("<|im_start|>")[-(n + 1) : -1]
    return "\n".join(t.replace("<|im_end|>", "").strip()[-600:] for t in turns)


def write_label_sheet(
    rows: Sequence[Mapping[str, Any]],
    completions: Mapping[str, str],
    prompts: Mapping[str, str],
    out: Path,
    n: int,
    seed: int,
) -> int:
    wrong = [r for r in rows if classify(r) is not None]
    sample = random.Random(seed).sample(wrong, min(n, len(wrong)))
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["step_id", "auto_bucket", "human_bucket", "reference_tools", "completion", "context"]
        )
        for r in sample:
            w.writerow(
                [
                    r["step_id"],
                    classify(r),
                    "",
                    " ".join(r.get("reference_tools") or ()) or "(reply)",
                    completions.get(r["step_id"], "")[:1500],
                    _tail(prompts.get(r["step_id"], "")),
                ]
            )
    return len(sample)


def check_labels(sheet: Path) -> dict[str, Any]:
    rows = list(csv.DictReader(sheet.open()))
    labelled = [r for r in rows if r["human_bucket"].strip()]
    unknown = {r["human_bucket"].strip() for r in labelled} - set(BUCKETS) - set(OUTSIDE)
    if unknown:
        raise ValueError(f"unknown human_bucket values {sorted(unknown)}; use {BUCKETS + OUTSIDE}")
    agree = sum(r["human_bucket"].strip() == r["auto_bucket"] for r in labelled)
    return {
        "labelled": len(labelled),
        "agreement": round(agree / len(labelled), 4) if labelled else None,
        "passes_90pct": bool(labelled) and agree / len(labelled) >= 0.9,
        "disagreements": [
            (r["step_id"], r["auto_bucket"], r["human_bucket"])
            for r in labelled
            if r["human_bucket"].strip() != r["auto_bucket"]
        ],
    }


def main(argv: list[str] | None = None) -> int:
    from src.cli import build_parser, resolve
    from src.runlog import read_records

    parser = build_parser("Failure taxonomy over forced-eval results")
    parser.add_argument("--results", type=Path, default=Path("results/forced-teacher-c16.jsonl"))
    parser.add_argument("--fingerprint", help="cell identity prefix when a log holds several")
    parser.add_argument("--label-sheet", type=int, metavar="N", help="write N wrong steps to label")
    parser.add_argument("--completions", type=Path)
    parser.add_argument("--prompts", type=Path)
    parser.add_argument("--check-labels", type=Path, metavar="CSV")
    args = parser.parse_args(argv)
    cfg = resolve(args)

    if args.check_labels:
        print(json.dumps(check_labels(args.check_labels), indent=2))
        return 0
    if not args.results.exists():
        print(f"[taxonomy] {args.results} does not exist; nothing to classify")
        return 0

    rows = forced_rows(args.results, args.fingerprint)
    table = bucket_table(rows)
    out = cfg.paths.results_dir / f"taxonomy-{args.results.stem}.json"
    out.write_text(json.dumps({"source": str(args.results), **table}, indent=2) + "\n")
    print(json.dumps(table, indent=2))
    print(f"[taxonomy] wrote {out}")

    if args.label_sheet:
        if not (args.completions and args.prompts):
            parser.error("--label-sheet needs --completions and --prompts")
        comps = {r["step_id"]: r["completion"] for r in read_records(args.completions)}
        prompts = {r["step_id"]: r["prompt"] for r in read_records(args.prompts)}
        sheet = cfg.paths.results_dir / f"label-sheet-{args.results.stem}.csv"
        n = write_label_sheet(rows, comps, prompts, sheet, args.label_sheet, cfg.seed)
        print(f"[taxonomy] wrote {n} steps to label -> {sheet} (fill the human_bucket column)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
