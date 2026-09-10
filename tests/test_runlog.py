"""Resumability: a killed session must not cost a re-run (plan §7 constraint 1)."""

from __future__ import annotations

import json

import pytest

from src.runlog import RunLog, read_records

CELLS = [("task_1", 0), ("task_1", 1), ("task_2", 0)]


def _log(tmp_path, fingerprint="fp_abc", **kw):
    return RunLog(tmp_path / "r.jsonl", config_fingerprint=fingerprint, config_name="demo", **kw)


def _sweep(log) -> int:
    """Run every cell that isn't done. Returns how many actually ran."""
    ran = 0
    for task_id, seed in CELLS:
        if log.should_run(task_id, seed):
            log.append(task_id=task_id, seed=seed, payload={"success": True})
            ran += 1
    return ran


# --- the acceptance test named in division-of-labor.md ----------------------


def test_second_run_does_zero_work(tmp_path):
    """ACCEPTANCE: "Run twice, second run does zero work."."""
    log = _log(tmp_path)
    assert _sweep(log) == len(CELLS)
    assert _sweep(log) == 0


def test_resume_after_partial_sweep(tmp_path):
    """The realistic case: the session dies halfway through."""
    log = _log(tmp_path)
    log.append(task_id="task_1", seed=0, payload={"success": True})
    assert _sweep(log) == len(CELLS) - 1


def test_a_fresh_log_object_sees_prior_work(tmp_path):
    """Resume happens in a new process, so state must live in the file."""
    _sweep(_log(tmp_path))
    assert _sweep(_log(tmp_path)) == 0


# --- fingerprint scoping -----------------------------------------------------


def test_every_record_carries_the_fingerprint(tmp_path):
    log = _log(tmp_path)
    _sweep(log)
    assert all(r["config_fingerprint"] == "fp_abc" for r in read_records(log.path))


def test_a_different_config_starts_fresh_cells(tmp_path):
    _sweep(_log(tmp_path, fingerprint="fp_abc"))
    other = _log(tmp_path, fingerprint="fp_xyz")
    assert other.completed_cells() == set()
    assert _sweep(other) == len(CELLS)


def test_old_records_are_preserved_not_overwritten(tmp_path):
    _sweep(_log(tmp_path, fingerprint="fp_abc"))
    _sweep(_log(tmp_path, fingerprint="fp_xyz"))
    fingerprints = {r["config_fingerprint"] for r in read_records(tmp_path / "r.jsonl")}
    assert fingerprints == {"fp_abc", "fp_xyz"}


def test_a_log_cannot_exist_without_a_fingerprint(tmp_path):
    """Red flag: "results written without a config fingerprint attached"."""
    with pytest.raises(ValueError, match="fingerprint is required"):
        RunLog(tmp_path / "r.jsonl", config_fingerprint="", config_name="demo")


def test_payload_cannot_forge_a_reserved_field(tmp_path):
    log = _log(tmp_path)
    with pytest.raises(ValueError, match="reserved keys"):
        log.append(task_id="t", seed=0, payload={"config_fingerprint": "forged"})


def test_smoke_records_are_marked(tmp_path):
    log = _log(tmp_path, smoke=True)
    record = log.append(task_id="t", seed=0, payload={})
    assert record["smoke"] is True


# --- crash tolerance ---------------------------------------------------------


def test_truncated_final_line_is_tolerated(tmp_path):
    """What a session killed mid-write leaves behind. Must not block a restart."""
    log = _log(tmp_path)
    log.append(task_id="task_1", seed=0, payload={"success": True})
    with log.path.open("a") as handle:
        handle.write('{"config_fingerprint": "fp_abc", "task_id": "task_2", "se')

    assert log.completed_cells() == {("task_1", 0)}
    assert _sweep(log) == len(CELLS) - 1


def test_corruption_before_the_last_line_is_an_error(tmp_path):
    """Mid-file corruption is not a partial write — surface it, don't guess."""
    log = _log(tmp_path)
    log.append(task_id="task_1", seed=0, payload={"success": True})
    lines = log.path.read_text().splitlines()
    log.path.write_text("not json at all\n" + "\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="corrupt"):
        log.completed_cells()


def test_missing_file_reads_as_empty(tmp_path):
    assert list(read_records(tmp_path / "absent.jsonl")) == []
    assert _log(tmp_path).completed_cells() == set()


def test_records_are_valid_jsonl(tmp_path):
    log = _log(tmp_path)
    _sweep(log)
    for line in log.path.read_text().splitlines():
        json.loads(line)
