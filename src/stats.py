"""Task-success statistics: pass^k and confidence intervals across seeds.

Pure Python (no scipy): the light CI environment runs it, and the only
distribution needed is Student's t at small degrees of freedom.

    python src/stats.py baseline      # the teacher row of the README table
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Task-success statistics")
    parser.add_argument("what", choices=["baseline"])
    parser.add_argument("--config", default="configs/qwen05b.yaml")
    args = parser.parse_args(argv)

    from src.config import load_config
    from src.runlog import read_records

    cfg = load_config(args.config)
    log = cfg.paths.results_dir / "harvest-baseline.jsonl"
    fp = cfg.teacher_fingerprint()
    records = [r for r in read_records(log) if r["config_fingerprint"] == fp and not r.get("smoke")]
    table = {
        "source": str(log),
        "teacher": cfg.teacher.model,
        "catalog": 16,
        **success_table(records),
    }
    out = cfg.paths.results_dir / "baseline_summary.json"
    out.write_text(json.dumps(table, indent=2) + "\n")
    print(json.dumps(table, indent=2))
    print(f"[stats] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
