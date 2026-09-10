"""Config schema, `extends` merge, smoke overrides, fingerprint."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config import SMOKE_OVERRIDES, ExperimentConfig, load_config

REAL = "configs/qwen05b.yaml"


def test_extends_pulls_shared_values_from_base():
    cfg = load_config(REAL)
    assert cfg.name == "qwen05b"
    assert cfg.teacher.provider == "google"  # inherited from base.yaml
    assert cfg.model.base_model == "Qwen/Qwen2.5-0.5B-Instruct"  # own value


def test_smoke_and_real_validate_against_one_schema():
    """Proof the two paths did not fork: same class, same validation."""
    real = load_config(REAL)
    smoke = load_config(REAL, smoke=True)
    assert isinstance(real, ExperimentConfig)
    assert isinstance(smoke, ExperimentConfig)
    assert type(real) is type(smoke)


def test_smoke_shrinks_the_inputs_only():
    real, smoke = load_config(REAL), load_config(REAL, smoke=True)
    assert smoke.smoke is True and real.smoke is False
    assert smoke.train.epochs < real.train.epochs
    assert smoke.eval.n_tasks < real.eval.n_tasks
    assert len(smoke.eval.seeds) < len(real.eval.seeds)
    # the model itself is untouched — same code path, tiny inputs
    assert smoke.model == real.model


def test_smoke_overrides_are_declared_in_one_place():
    smoke = load_config(REAL, smoke=True)
    assert smoke.train.epochs == SMOKE_OVERRIDES["train"]["epochs"]
    assert smoke.eval.n_tasks == SMOKE_OVERRIDES["eval"]["n_tasks"]


def test_fingerprint_separates_smoke_from_real():
    """Scaffold numbers must never be mistakable for a real run's."""
    assert load_config(REAL).fingerprint() != load_config(REAL, smoke=True).fingerprint()


def test_fingerprint_is_stable_across_calls():
    assert load_config(REAL).fingerprint() == load_config(REAL).fingerprint()


def test_fingerprint_ignores_paths():
    """Where results land is not part of what was run."""
    base = load_config(REAL)
    moved = base.model_copy(
        update={"paths": base.paths.model_copy(update={"results_dir": "/tmp/x"})}
    )
    assert base.fingerprint() == moved.fingerprint()


def test_fingerprint_tracks_a_hyperparameter_change():
    base = load_config(REAL)
    changed = base.model_copy(update={"train": base.train.model_copy(update={"lora_r": 32})})
    assert base.fingerprint() != changed.fingerprint()


def test_train_config_has_no_catalog_field():
    """Plan §5: one adapter, all three catalogs. The field must not exist."""
    assert not [f for f in type(load_config(REAL).train).model_fields if "catalog" in f.lower()]


def test_catalog_is_an_eval_axis():
    assert load_config(REAL).eval.catalog_sizes == [15, 40, 80]


def test_config_is_frozen():
    cfg = load_config(REAL)
    with pytest.raises(ValidationError):
        cfg.seed = 99


def test_typo_in_yaml_is_an_error_not_a_silent_default(tmp_path):
    """extra="forbid": a misspelled key must fail loudly, not be ignored."""
    (tmp_path / "base.yaml").write_text(Path("configs/base.yaml").read_text())
    bad = tmp_path / "bad.yaml"
    bad.write_text("extends: base.yaml\nname: typo\nlearningrate: 3\n")
    with pytest.raises(ValidationError):
        load_config(bad)


def test_seed_is_required(tmp_path):
    cfg = tmp_path / "noseed.yaml"
    cfg.write_text("name: noseed\nprompt_template_hash: deadbeef\n")
    with pytest.raises(ValidationError):
        load_config(cfg)


def test_duplicate_eval_seeds_rejected(tmp_path):
    src = Path("configs/base.yaml").read_text()
    bad = tmp_path / "base.yaml"
    bad.write_text(src.replace("seeds: [0, 1, 2, 3, 4]", "seeds: [0, 0, 1]"))
    with pytest.raises(ValidationError, match="duplicates"):
        load_config(bad)


def test_circular_extends_is_caught(tmp_path):
    (tmp_path / "a.yaml").write_text("extends: b.yaml\nname: a\n")
    (tmp_path / "b.yaml").write_text("extends: a.yaml\nname: b\n")
    with pytest.raises(ValueError, match="circular"):
        load_config(tmp_path / "a.yaml")


def test_seed_override_from_cli_wins():
    assert load_config(REAL, seed=4242).seed == 4242
