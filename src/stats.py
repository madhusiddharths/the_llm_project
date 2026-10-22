"""Task-success statistics: pass^k and confidence intervals across seeds.

Pure Python (no scipy): the light CI environment runs it, and the only
distribution needed is Student's t at small degrees of freedom.

    python src/stats.py baseline                # the teacher row of the README table
    python src/stats.py baseline --catalog 80   # D7 padded-catalog baselines
    python src/stats.py free --markdown         # Stage A and D, from results/free-*.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NATIVE_CATALOG = 16  # the unpadded baseline, logged without a catalog tag

# Two-sided 95% critical values of Student's t, by degrees of freedom.
_T95 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
}


def t95(df: int) -> float:
    if df < 1:
        raise ValueError("a confidence interval needs at least two seeds")
    return _T95.get(df, 1.96 if df > 30 else 2.042)


def mean_ci(values: Sequence[float]) -> dict[str, float]:
    """Mean and 95% t-interval across seeds (plan §6: CIs across seeds)."""
    n = len(values)
    mean = statistics.fmean(values)
    half = t95(n - 1) * statistics.stdev(values) / math.sqrt(n)
    return {
        "mean": round(mean, 4),
        "ci95_low": round(mean - half, 4),
        "ci95_high": round(mean + half, 4),
        "n": n,
    }


def pass_hat_k(outcomes: Mapping[str, Sequence[bool]], k: int) -> float:
    """tau-bench's pass^k: the chance that k i.i.d. trials of a task ALL succeed,
    averaged over tasks. Unbiased per task as C(c, k) / C(n, k) for c successes
    in n trials. pass^1 is plain mean success; pass^5 over 5 seeds is "solved
    on every seed" (plan §3)."""
    scores = []
    for trials in outcomes.values():
        n, c = len(trials), sum(trials)
        if n < k:
            raise ValueError(f"pass^{k} needs at least {k} trials per task, got {n}")
        scores.append(math.comb(c, k) / math.comb(n, k))
    return round(statistics.fmean(scores), 4)


def success_table(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Task success from RunLog records with `task_id`, `seed` (rep) and `reward`."""
    by_task: dict[str, list[bool]] = defaultdict(list)
    by_seed: dict[int, list[bool]] = defaultdict(list)
    for r in records:
        ok = r["reward"] == 1.0
        by_task[str(r["task_id"])].append(ok)
        by_seed[int(r["seed"])].append(ok)
    n_trials = {len(v) for v in by_task.values()}
    if len(n_trials) != 1:
        raise ValueError(
            f"tasks have unequal trial counts {sorted(n_trials)}; pass^k would be biased"
        )
    k_max = n_trials.pop()
    per_seed = {s: round(sum(v) / len(v), 4) for s, v in sorted(by_seed.items())}
    return {
        "n_tasks": len(by_task),
        "n_episodes": sum(len(v) for v in by_task.values()),
        "task_success": mean_ci(list(per_seed.values())),
        "per_seed": per_seed,
        **{f"pass^{k}": pass_hat_k(by_task, k) for k in range(1, k_max + 1)},
        "tasks_solved_any_seed": sum(any(v) for v in by_task.values()),
        "tasks_solved_every_seed": sum(all(v) for v in by_task.values()),
    }


# Records written before 2026-10-22 carry no "simulator" field; they all faced
# base.yaml's simulator, the only one there was.
V1_SIMULATOR = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"

# results/free-<label>-c<catalog>.jsonl; Stage D labels end in -esc<threshold>.
_FREE_FILE = re.compile(r"^free-(?P<label>.+?)(?:-esc(?P<t>[0-9.]+))?-c(?P<catalog>\d+)\.jsonl$")


def free_row(path: Path, records: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """One results file's row: its success table, or a plain count while the cell
    is still filling (tasks with unequal trials would bias pass^k and the CI)."""
    m = _FREE_FILE.match(path.name)
    if m is None:
        return None
    fps = {r["config_fingerprint"] for r in records}
    if len(fps) > 1:
        raise ValueError(f"{path} holds {len(fps)} identities; a cell must hold one")
    sims = {r.get("simulator", V1_SIMULATOR) for r in records}
    if len(sims) > 1:
        raise ValueError(f"{path} holds episodes from {len(sims)} simulators: {sorted(sims)}")
    turns = sum(r.get("student_turns", 0) for r in records)
    row: dict[str, Any] = {
        "label": m["label"],
        "simulator": sims.pop(),
        "catalog": int(m["catalog"]),
        "threshold": float(m["t"]) if m["t"] else None,
        "escalation_rate": (
            round(sum(r.get("escalated_turns", 0) for r in records) / turns, 4) if turns else None
        ),
        "degenerate_episodes": sum(1 for r in records if r.get("degenerate_user_turn")),
    }
    try:
        return {**row, "complete": True, **success_table(records)}
    except ValueError:
        wins = sum(r["reward"] == 1.0 for r in records)
        return {**row, "complete": False, "n_episodes": len(records), "successes": wins}


def free_tables(results_dir: Path) -> list[dict[str, Any]]:
    from src.runlog import read_records

    rows = []
    for path in sorted(results_dir.glob("free-*.jsonl")):
        records = [r for r in read_records(path) if not r.get("smoke")]
        if records and (row := free_row(path, records)) is not None:
            rows.append(row)
    return rows


def _pct(row: Mapping[str, Any]) -> str:
    if not row["complete"]:
        return f"{row['successes']}/{row['n_episodes']} so far"
    ts = row["task_success"]
    return f"{ts['mean']:.1%} ({ts['ci95_low']:.1%} to {ts['ci95_high']:.1%})"


def free_markdown(rows: Sequence[Mapping[str, Any]], teacher: Mapping[int, Any]) -> str:
    """README tables: Stage A (model x catalog) and, once it exists, Stage D."""
    catalogs = sorted({r["catalog"] for r in rows if r["threshold"] is None} | set(teacher))
    lines = [
        "| Model | " + " | ".join(f"c{c}" for c in catalogs) + " |",
        "|---|" + "---|" * len(catalogs),
    ]
    for label in sorted({r["label"] for r in rows if r["threshold"] is None}):
        cell = {r["catalog"]: r for r in rows if r["label"] == label and r["threshold"] is None}
        lines.append(
            f"| {label} | "
            + " | ".join(_pct(cell[c]) if c in cell else "" for c in catalogs)
            + " |"
        )
    if teacher:
        lines.append(
            "| teacher | "
            + " | ".join(
                _pct({"complete": True, **teacher[c]}) if c in teacher else "" for c in catalogs
            )
            + " |"
        )
    stage_d = sorted((r for r in rows if r["threshold"] is not None), key=lambda r: r["threshold"])
    if stage_d:
        lines += [
            "",
            "| Model | Catalog | Threshold | Escalated turns | Task success |",
            "|---|---|---|---|---|",
        ]
        for r in stage_d:
            rate = "" if r["escalation_rate"] is None else f"{r['escalation_rate']:.1%}"
            lines.append(
                f"| {r['label']} | {r['catalog']} | {r['threshold']:.2f} | {rate} | {_pct(r)} |"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Task-success statistics")
    parser.add_argument("what", choices=["baseline", "free"])
    parser.add_argument("--config", default="configs/qwen05b.yaml")
    parser.add_argument("--catalog", type=int, default=NATIVE_CATALOG, choices=[16, 40, 80])
    parser.add_argument("--markdown", action="store_true", help="free: README-ready tables")
    args = parser.parse_args(argv)

    from src.config import load_config
    from src.runlog import read_records

    cfg = load_config(args.config)
    if args.what == "free":
        return _free(cfg, args.markdown)
    if args.catalog == NATIVE_CATALOG:
        tag, fp = "", cfg.teacher_fingerprint()
    else:
        tag, fp = f"-c{args.catalog}", cfg.padded_baseline_fingerprint(args.catalog)
    log = cfg.paths.results_dir / f"harvest-baseline{tag}.jsonl"
    epoch = f"-{cfg.sim_tag}" if cfg.sim_tag else ""
    records = [r for r in read_records(log) if r["config_fingerprint"] == fp and not r.get("smoke")]
    if not records:
        raise SystemExit(f"[stats] no records for fingerprint {fp[:12]} in {log}")
    table = {
        "source": str(log),
        "teacher": cfg.teacher.model,
        "simulator": cfg.simulator.model,
        "catalog": args.catalog,
        **success_table(records),
    }
    out = cfg.paths.results_dir / f"baseline{epoch}{tag}_summary.json"
    out.write_text(json.dumps(table, indent=2) + "\n")
    print(json.dumps(table, indent=2))
    print(f"[stats] wrote {out}")
    return 0


def _free(cfg, markdown: bool) -> int:
    """The tables for ONE simulator epoch: the config's. Students and teacher
    are only ever compared under the same simulator."""
    results_dir = cfg.paths.results_dir
    rows = [r for r in free_tables(results_dir) if r["simulator"] == cfg.simulator.model]
    if not rows:
        print(
            f"[stats] no free-*.jsonl results under simulator {cfg.simulator.model} in "
            f"{results_dir}; run `make pull-free` first"
        )
        return 1
    epoch = f"-{cfg.sim_tag}" if cfg.sim_tag else ""
    teacher = {}
    for c in (16, 40, 80):
        tag = "" if c == NATIVE_CATALOG else f"-c{c}"
        f = results_dir / f"baseline{epoch}{tag}_summary.json"
        if f.exists():
            teacher[c] = json.loads(f.read_text())
    out = results_dir / f"free{epoch}_summary.json"
    out.write_text(json.dumps(rows, indent=2) + "\n")
    print(free_markdown(rows, teacher) if markdown else json.dumps(rows, indent=2))
    print(f"[stats] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
