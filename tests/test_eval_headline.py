"""Stage I runner: the pinned harness, the cells and their order, and when a
night's run stops. No tau2, no model, no API: cells are launched by a stub."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from src.config import load_config
from src.eval_free import cell_identity
from src.eval_headline import (
    DEFAULT_SPEC,
    build_cells,
    eval_free_command,
    load_spec,
    pinned_harness,
    run_cells,
)
from src.hashing import hash_obj
from src.invariants import InvariantViolation

SPEC = load_spec(DEFAULT_SPEC)


def _cfg(results: Path, smoke: bool = False):
    cfg = load_config(SPEC.config, smoke=smoke)
    return cfg.model_copy(update={"paths": cfg.paths.model_copy(update={"results_dir": results})})


def _baseline(results: Path, fingerprint: str, smoke: bool = False) -> None:
    rec = {"config_fingerprint": fingerprint, "task_id": "1", "seed": 0, "smoke": smoke}
    (results / "harvest-baseline-c80.jsonl").write_text(json.dumps(rec) + "\n")


def _log_episodes(cell, reps, n_tasks):
    for rep in reps:
        for t in range(n_tasks):
            cell.log.append(task_id=str(t), seed=rep, payload={"reward": 1.0})


def test_the_repo_spec_pins_what_its_config_computes():
    # A config edit that moves the simulator, judge or teacher fails here first.
    assert load_config(SPEC.config).teacher_fingerprint() == SPEC.harness


def test_the_pin_needs_a_non_smoke_baseline_under_it(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(InvariantViolation, match="teacher baseline"):
        pinned_harness(cfg, SPEC)
    _baseline(tmp_path, cfg.padded_baseline_fingerprint(80), smoke=True)
    with pytest.raises(InvariantViolation):
        pinned_harness(cfg, SPEC)
    _baseline(tmp_path, cfg.padded_baseline_fingerprint(80))
    assert pinned_harness(cfg, SPEC) == SPEC.harness


def test_a_moved_harness_is_refused_even_with_a_baseline_under_it(tmp_path):
    # The v1 config has its own baseline, but it is not the pinned epoch.
    v1 = load_config("configs/qwen3b.yaml")
    v1 = v1.model_copy(update={"paths": v1.paths.model_copy(update={"results_dir": tmp_path})})
    _baseline(tmp_path, v1.padded_baseline_fingerprint(80))
    with pytest.raises(InvariantViolation, match="pinned to"):
        pinned_harness(v1, SPEC)


def test_cells_run_method_major_in_spec_order_and_sft_shares_free_mlx_files(tmp_path):
    cfg = _cfg(tmp_path)
    cells = build_cells(cfg, SPEC, SPEC.harness, "mlx")
    assert [(c.method, c.catalog) for c in cells] == [
        (m.name, c) for m in SPEC.methods for c in SPEC.catalogs
    ]
    sft80 = next(c for c in cells if (c.method, c.catalog) == ("sft", 80))
    # `make free-mlx CFG=qwen3b SIM=nemotron35 CATALOGS=80` writes exactly this cell.
    assert sft80.log.path == tmp_path / f"free-{cfg.name}-sft-c80.jsonl"
    identity = cell_identity(cfg, SPEC.harness, 80, sft80.adapter, "mlx", None)
    assert sft80.log.fingerprint == hash_obj(identity)
    assert len({c.log.fingerprint for c in cells}) == len(cells)


def test_progress_counts_only_the_first_n_seeds(tmp_path):
    spec = replace(SPEC, n_seeds=3)
    cell = build_cells(_cfg(tmp_path), spec, SPEC.harness, "mlx")[0]
    assert cell.total == 60 * 3
    _log_episodes(cell, reps=[0, 1, 3], n_tasks=60)  # rep 3 is outside a 3-seed run
    assert cell.done() == 120 and not cell.complete
    _log_episodes(cell, reps=[2], n_tasks=60)
    assert cell.complete


def test_n_seeds_beyond_the_config_is_refused(tmp_path):
    with pytest.raises(InvariantViolation, match="n_seeds"):
        build_cells(_cfg(tmp_path), replace(SPEC, n_seeds=6), SPEC.harness, "mlx")


def test_the_command_is_eval_free_with_the_cells_label_and_seed_count(tmp_path):
    cell = build_cells(_cfg(tmp_path), SPEC, SPEC.harness, "mlx")[-1]
    cmd = eval_free_command(
        SPEC, cell, backend="mlx", concurrency=3, hub_sync="u/results", smoke=False
    )
    assert cmd[:2] == [sys.executable, "src/eval_free.py"]
    flags = dict(zip(cmd[2::2], cmd[3::2], strict=False))
    assert flags["--config"] == SPEC.config
    assert flags["--adapter"] == cell.adapter
    assert flags["--label"] == cell.tag
    assert flags["--n-seeds"] == str(SPEC.n_seeds)
    assert flags["--catalog"] == str(cell.catalog)
    assert flags["--hub-sync"] == "u/results"
    assert "--smoke" not in cmd


def test_the_run_moves_on_only_from_a_complete_cell(tmp_path):
    spec = replace(SPEC, n_seeds=1)
    cells = build_cells(_cfg(tmp_path), spec, SPEC.harness, "mlx")
    launched = []

    def finish(cell):
        launched.append((cell.method, cell.catalog))
        _log_episodes(cell, reps=[0], n_tasks=cell.n_tasks)
        return 0

    def half(cell):
        launched.append((cell.method, cell.catalog))
        _log_episodes(cell, reps=[0], n_tasks=cell.n_tasks // 2)
        return 0

    # Night 1: the first cell finishes, the second runs out of budget halfway.
    calls = iter([finish, half])
    assert run_cells(cells, lambda c: next(calls)(c), lambda _r: True) == 0
    assert launched == [("sft", 16), ("sft", 80)]
    # Night 2: resumes the unfinished cell, skips the finished one.
    launched.clear()
    calls = iter([finish, finish])
    assert run_cells(cells, lambda c: next(calls)(c), lambda r: "grpo" not in r) == 0
    # The GRPO adapter is not on the Hub yet: stop before it, launch nothing for it.
    assert launched == [("sft", 80)]


def test_a_failed_eval_free_stops_the_run_with_its_code(tmp_path):
    cells = build_cells(_cfg(tmp_path), SPEC, SPEC.harness, "mlx")
    seen = []
    assert run_cells(cells, lambda c: seen.append(c) or 1, lambda _r: True) == 1
    assert len(seen) == 1
