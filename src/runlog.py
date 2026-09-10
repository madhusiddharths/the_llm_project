"""Append-only result log with per-cell resume.

Plan §7, constraint 1: Kaggle sessions die at 12 hours. "Append results to JSONL
after every episode, and make the runner skip completed (config, task, seed)
cells on restart." Written on day one, as the plan insists, rather than after
losing a nine-hour run.

Also closes a Red Flag: "results written without a config fingerprint attached".
A RunLog cannot be constructed without a fingerprint, so an unstamped record is
not expressible.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

Cell = tuple[str, int]  # (task_id, seed), scoped to one config fingerprint

# Reserved keys the log stamps itself; a payload may not overwrite them.
_RESERVED = frozenset({"config_fingerprint", "config_name", "task_id", "seed", "ts", "smoke"})


def read_records(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield every record in a JSONL log.

    A truncated final line is tolerated and skipped: that is what a session
    killed mid-write leaves behind, and it must not block a restart. A malformed
    line anywhere earlier is real corruption and raises.
    """
    path = Path(path)
    if not path.exists():
        return

    lines = path.read_text().splitlines()
    last = len(lines) - 1
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            if index == last:
                return  # partial write from a killed session
            raise ValueError(f"{path}:{index + 1} is corrupt (not the final line)") from None


class RunLog:
    """Append-only JSONL scoped to one experiment cell's fingerprint."""

    def __init__(
        self, path: str | Path, *, config_fingerprint: str, config_name: str, smoke: bool = False
    ):
        if not config_fingerprint:
            raise ValueError(
                "config_fingerprint is required — results must be traceable to a config"
            )
        self.path = Path(path)
        self.fingerprint = config_fingerprint
        self.config_name = config_name
        self.smoke = smoke
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def completed_cells(self) -> set[Cell]:
        """(task_id, seed) pairs already finished under THIS fingerprint.

        Records from other fingerprints are ignored rather than deleted: a config
        change starts a fresh set of cells in the same file, and the old numbers
        stay auditable.
        """
        return {
            (str(rec["task_id"]), int(rec["seed"]))
            for rec in read_records(self.path)
            if rec.get("config_fingerprint") == self.fingerprint
            and rec.get("task_id") is not None
            and rec.get("seed") is not None
        }

    def should_run(self, task_id: str, seed: int, *, completed: set[Cell] | None = None) -> bool:
        """Cheap guard for a runner loop. Pass `completed` to avoid re-reading."""
        done = self.completed_cells() if completed is None else completed
        return (task_id, seed) not in done

    def append(self, *, task_id: str, seed: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Write one record and fsync it before returning.

        fsync per record is deliberate. The session can be killed between any two
        instructions, and an episode that ran but was not durably recorded gets
        recomputed on restart — which on a fixed GPU quota is spent budget.
        """
        clashes = _RESERVED & payload.keys()
        if clashes:
            raise ValueError(f"payload may not set reserved keys: {sorted(clashes)}")

        record = {
            "config_fingerprint": self.fingerprint,
            "config_name": self.config_name,
            "task_id": task_id,
            "seed": seed,
            "ts": time.time(),
            "smoke": self.smoke,
            **payload,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def __len__(self) -> int:
        return len(self.completed_cells())
