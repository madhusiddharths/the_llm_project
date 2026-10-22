"""Stage E1: seeded task-id subsets of the SFT set (plan X5, E1-1).

The data-scaling ablation trains 1.5B on 25% and 50% of the teacher data; the
100% point is the v1 adapter. Resolved 2026-09-22: subset by TASK ID, seeded,
over the teacher tasks that have at least one successful episode (46), and keep
every episode of a chosen task. Subsetting by episode would let one task's three
temperature variants straddle the cut, which measures how many near-duplicate
episodes help rather than how much distinct data does.

The subsets are nested: one seeded permutation, and each fraction takes a prefix
of it, so the 25% tasks are inside the 50% ones and the curve moves one variable.
The task count rounds half up (46 x 0.25 = 11.5 -> 12; x 0.5 -> 23).

Input is an existing SFT directory (train.jsonl + manifest.json from
build_sft.py); output is a new one whose manifest keeps the four keys train.py
checks and adds the fraction, the seed, the parent file's sha256 and the task
list. Runs anywhere in seconds, including a Kaggle notebook on the attached
dataset, so no new upload is needed.

    python src/subsets.py --config configs/v2/qwen15b-e1.yaml --data <sft dir> \\
        --fraction 0.25 --out /kaggle/working/sft-f25
"""

from __future__ import annotations

import json
import math
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cli import build_parser, resolve
from src.hashing import hash_file
from src.invariants import InvariantViolation, check_all

KEPT_KEYS = ("prompt_template_hash", "system_prompt_hash", "catalog_hash", "catalog_size")


def subset_task_ids(task_ids: list[str], fraction: float, seed: int) -> list[str]:
    """The first round-half-up(n * fraction) tasks of one seeded permutation."""
    if not 0 < fraction <= 1:
        raise ValueError(f"fraction must lie in (0, 1], got {fraction}")
    ordered = sorted(set(task_ids), key=lambda t: (len(t), t))
    permuted = random.Random(seed).sample(ordered, len(ordered))
    k = max(1, math.floor(len(permuted) * fraction + 0.5))
    return sorted(permuted[:k], key=lambda t: (len(t), t))


def write_subset(
    records: list[dict[str, Any]],
    manifest: dict[str, Any],
    *,
    fraction: float,
    seed: int,
    out_dir: Path,
) -> dict[str, Any]:
    chosen = set(subset_task_ids([r["task_id"] for r in records], fraction, seed))
    kept = [r for r in records if r["task_id"] in chosen]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "train.jsonl"
    out_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept))
    sub = {
        "file": out_path.name,
        "file_sha256": hash_file(out_path),
        "created": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "smoke": manifest.get("smoke", False),
        "teacher_fingerprint": manifest["teacher_fingerprint"],
        **{k: manifest[k] for k in KEPT_KEYS if k in manifest},
        "rejection_sampled": manifest.get("rejection_sampled", True),
        "ablation": {
            "fraction": fraction,
            "subset_seed": seed,
            "denominator": "teacher_tasks",
            "parent_file_sha256": manifest["file_sha256"],
            "parent_n_tasks": len({r["task_id"] for r in records}),
        },
        "n_episodes": len(kept),
        "n_decisions": sum(r["n_decisions"] for r in kept),
        "n_tasks": len(chosen),
        "task_ids": sorted(chosen, key=lambda t: (len(t), t)),
        "max_chars": max((r["chars"] for r in kept), default=0),
    }
    (out_dir / "manifest.json").write_text(json.dumps(sub, indent=2) + "\n")
    return sub


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("E1: seeded task-id subset of an SFT set")
    parser.add_argument("--data", type=Path, required=True, help="SFT dir (train.jsonl + manifest)")
    parser.add_argument("--fraction", type=float, required=True)
    args = parser.parse_args(argv)  # --out (the subset directory) comes from build_parser
    if args.out is None:
        parser.error("--out is required: the directory to write the subset to")

    cfg = resolve(args)
    print(f"[subsets] {', '.join(check_all(cfg))}")
    if cfg.ablation is None:
        parser.error(f"{args.config} sets no ablation section; use configs/v2/qwen15b-e1.yaml")
    if args.fraction not in cfg.ablation.fractions:
        raise InvariantViolation(
            f"fraction {args.fraction} is not in ablation.fractions {cfg.ablation.fractions}"
        )
    manifest = json.loads((args.data / "manifest.json").read_text())
    data_path = args.data / manifest["file"]
    if hash_file(data_path) != manifest["file_sha256"]:
        raise InvariantViolation(f"{data_path} does not match its manifest sha256")
    records = [json.loads(line) for line in data_path.read_text().splitlines() if line.strip()]
    sub = write_subset(
        records,
        manifest,
        fraction=args.fraction,
        seed=cfg.ablation.subset_seed,
        out_dir=args.out,
    )
    print(
        f"[subsets] {args.fraction:.0%}: {sub['n_tasks']}/{sub['ablation']['parent_n_tasks']} tasks, "
        f"{sub['n_episodes']} episodes, {sub['n_decisions']} decisions -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
