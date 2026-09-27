"""Forced-mode escalation preview: does low confidence find the wrong steps? (V1-18)

Stage D spends teacher requests on free-running episodes. Before that, this
answers the cheaper question on data already on disk: in teacher-forced replay,
if every step below a threshold went to the teacher, how many steps would
escalate and how many of the student's mistakes would that catch?

Inputs are one completions file generated with `generate.py --logprobs` and
the forced-eval results for that same file (`eval_forced.py --backend file`).
Confidence comes from src/logprobs.py; nothing here re-derives it.

Two signals, side by side:
  name-only   the plan's original: tool-name confidence; a reply never escalates
  step        the decided one (2026-09-25): min(call-vs-reply, tool name)
Both escalate a step with no measurable confidence (it did not parse).

A step is RIGHT if it matches the reference: the normalized call on a tool-call
step, the reply decision on a reply step. An escalated step counts as right,
because in forced mode the teacher's action IS the reference. That makes the
"after escalation" numbers an upper bound for what Stage D can recover; Stage
D measures the real thing, with the teacher answering live.

    python src/escalation_sim.py \\
        --completions results/completions-qwen15b-sft-c80-logprobs.jsonl \\
        --results results/forced-qwen15b-sft-lp-c80.jsonl
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cli import build_parser, resolve
from src.logprobs import TOOL_CALL_TOKEN, StepConfidence, hf_decoder, score_completion
from src.runlog import read_records

SIGNALS = ("name_only", "step")


def step_right(row: Mapping[str, Any]) -> bool:
    if row["reference_kind"] == "tool_call":
        return bool(row["normalized_match"])
    return bool(row["kind_match"])


def signal(conf: StepConfidence, which: str) -> float | None:
    """The number compared with the threshold; None means "always escalate"."""
    if conf.kind == "unparsed":
        return None
    if which == "name_only":
        return 1.0 if conf.kind == "reply" else conf.name_confidence
    return conf.step_confidence


def auroc(scores: Sequence[float], wrong: Sequence[bool]) -> float | None:
    """P(a random wrong step has LOWER confidence than a random right one); ties
    count half. 0.5 is a useless signal, 1.0 a perfect one."""
    bad = [s for s, w in zip(scores, wrong, strict=True) if w]
    good = [s for s, w in zip(scores, wrong, strict=True) if not w]
    if not bad or not good:
        return None
    wins = sum((b < g) + 0.5 * (b == g) for b in bad for g in good)
    return wins / (len(bad) * len(good))


def sweep(
    confs: Mapping[str, StepConfidence],
    rows: Mapping[str, Mapping[str, Any]],
    thresholds: Sequence[float],
    which: str,
) -> dict[str, Any]:
    ids = sorted(set(confs) & set(rows))
    right = {i: step_right(rows[i]) for i in ids}
    n, n_wrong = len(ids), sum(not r for r in right.values())
    out: dict[str, Any] = {"n_steps": n, "accuracy_before": round(1 - n_wrong / n, 4)}
    measured = [i for i in ids if signal(confs[i], which) is not None]
    out["auroc"] = auroc(
        [signal(confs[i], which) or 0.0 for i in measured], [not right[i] for i in measured]
    )
    out["unmeasured_steps"] = n - len(measured)
    points = []
    for t in thresholds:
        esc = {i for i in ids if (s := signal(confs[i], which)) is None or s < t}
        caught = sum(1 for i in esc if not right[i])
        points.append(
            {
                "threshold": t,
                "escalation_rate": round(len(esc) / n, 4),
                "accuracy_after": round((n - n_wrong + caught) / n, 4),
                "mistakes_caught": round(caught / n_wrong, 4) if n_wrong else None,
                "wasted_escalations": round((len(esc) - caught) / len(esc), 4) if esc else None,
            }
        )
    out["points"] = points
    return out


def load_confidences(
    completions: Path, decode: Callable[[Sequence[int]], str], call_id: int, limit: int | None
) -> dict[str, StepConfidence]:
    out = {}
    for rec in read_records(completions):
        if "token_ids" not in rec:
            raise ValueError(f"{completions} has no token_ids: was it generated with --logprobs?")
        alts = {int(t): float(lp) for t, lp in rec.get("first_top") or []}
        out[rec["step_id"]] = score_completion(
            rec["token_ids"],
            rec["token_logprobs"],
            decode,
            first_alternatives=alts or None,
            call_token_id=call_id,
        )
        if limit is not None and len(out) >= limit:
            break
    return out


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("Forced-mode escalation preview from log-probs")
    parser.add_argument("--completions", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True, help="forced-eval results JSONL")
    parser.add_argument("--fingerprint", help="cell identity prefix when a log holds several")
    args = parser.parse_args(argv)
    cfg = resolve(args)
    for path in (args.completions, args.results):
        if not path.exists():
            print(f"[escalation_sim] {path} does not exist; nothing to do")
            return 0

    from transformers import AutoTokenizer

    from src.taxonomy import forced_rows

    tok = AutoTokenizer.from_pretrained(cfg.model.base_model)
    call_id = tok.convert_tokens_to_ids(TOOL_CALL_TOKEN)
    confs = load_confidences(
        args.completions, hf_decoder(tok), call_id, limit=40 if cfg.smoke else None
    )
    rows = {r["step_id"]: r for r in forced_rows(args.results, args.fingerprint)}
    thresholds = list(cfg.escalation.thresholds)
    report = {
        "completions": str(args.completions),
        "results": str(args.results),
        "smoke": cfg.smoke,
        **{w: sweep(confs, rows, thresholds, w) for w in SIGNALS},
    }
    for w in SIGNALS:
        r = report[w]
        print(
            f"\n[{w}] {r['n_steps']} steps, accuracy {r['accuracy_before']:.1%}, AUROC {r['auroc']}"
        )
        for p in r["points"]:
            print(
                f"  t={p['threshold']:.2f}  escalate {p['escalation_rate']:6.1%}  "
                f"accuracy {p['accuracy_after']:6.1%}  catches {p['mistakes_caught']}"
            )
    suffix = ".smoke" if cfg.smoke else ""
    out = args.out or cfg.paths.results_dir / f"escalation-sim-{args.completions.stem}{suffix}.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\n[escalation_sim] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
