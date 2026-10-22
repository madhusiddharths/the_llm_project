"""E1 subsets: seeded, nested, by task id, and accepted by train.py's checks."""

from __future__ import annotations

import json

from src.hashing import hash_file
from src.subsets import subset_task_ids, write_subset

TASKS = [str(i) for i in range(46)]


def test_the_counts_round_half_up_over_46_tasks():
    assert len(subset_task_ids(TASKS, 0.25, 7)) == 12
    assert len(subset_task_ids(TASKS, 0.5, 7)) == 23
    assert len(subset_task_ids(TASKS, 1.0, 7)) == 46


def test_subsets_are_nested_and_seeded():
    quarter, half = set(subset_task_ids(TASKS, 0.25, 7)), set(subset_task_ids(TASKS, 0.5, 7))
    assert quarter < half
    assert subset_task_ids(TASKS, 0.25, 7) == subset_task_ids(list(reversed(TASKS)), 0.25, 7)
    assert set(subset_task_ids(TASKS, 0.25, 8)) != quarter


def test_every_episode_of_a_chosen_task_is_kept(tmp_path):
    records = [
        {"task_id": t, "rep": r, "n_decisions": 3, "chars": 10, "segments": [["x", True]]}
        for t in TASKS
        for r in range(3)
    ]
    manifest = {
        "file_sha256": "parent",
        "teacher_fingerprint": "tf",
        "prompt_template_hash": "p",
        "system_prompt_hash": "s",
        "catalog_hash": "c",
        "catalog_size": 16,
    }
    sub = write_subset(records, manifest, fraction=0.25, seed=7, out_dir=tmp_path)
    kept = [json.loads(line) for line in (tmp_path / "train.jsonl").read_text().splitlines()]
    assert sub["n_tasks"] == 12 and len(kept) == 36  # 3 variants per task, never split
    assert {r["task_id"] for r in kept} == set(sub["task_ids"])
    written = json.loads((tmp_path / "manifest.json").read_text())
    # the keys train.load_sft checks are carried over unchanged
    for key in (
        "teacher_fingerprint",
        "prompt_template_hash",
        "system_prompt_hash",
        "catalog_hash",
    ):
        assert written[key] == manifest[key]
    assert written["file_sha256"] == hash_file(tmp_path / "train.jsonl")
    assert written["ablation"]["fraction"] == 0.25
