"""Pinned catalogs and system prompt: an edited file must stop a run."""

from __future__ import annotations

import pytest

from src.catalogs import PinMismatch, catalog_path, load_catalog, load_system_prompt, write_catalog
from src.config import load_config
from src.hashing import sha256_str

REAL = "configs/qwen05b.yaml"


def _tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def _cfg(tmp_path, **eval_overrides):
    cfg = load_config(REAL)
    return cfg.model_copy(
        update={
            "paths": cfg.paths.model_copy(update={"catalogs_dir": tmp_path}),
            "eval": cfg.eval.model_copy(update=eval_overrides),
        }
    )


def test_the_committed_snapshot_matches_its_pins():
    cfg = load_config(REAL)
    assert len(load_catalog(cfg, 16)) == 16
    assert "<policy>" in load_system_prompt(cfg)


def test_an_edited_catalog_is_refused(tmp_path):
    tools = [_tool("a"), _tool("b")]
    digest = write_catalog(catalog_path(tmp_path, 2), tools, [{"name": "a"}, {"name": "b"}])
    cfg = _cfg(tmp_path, catalog_hashes={2: digest})
    assert load_catalog(cfg, 2) == tools

    path = catalog_path(tmp_path, 2)
    path.write_text(path.read_text().replace('"a"', '"z"'))
    with pytest.raises(PinMismatch, match="pinned hash"):
        load_catalog(cfg, 2)


def test_an_unpinned_catalog_is_refused(tmp_path):
    write_catalog(
        catalog_path(tmp_path, 2), [_tool("a"), _tool("b")], [{"name": "a"}, {"name": "b"}]
    )
    with pytest.raises(PinMismatch, match="not pinned"):
        load_catalog(_cfg(tmp_path, catalog_hashes={}), 2)


def test_duplicate_tool_names_cannot_be_written(tmp_path):
    with pytest.raises(ValueError, match="duplicate"):
        write_catalog(catalog_path(tmp_path, 2), [_tool("a"), _tool("a")], [{"name": "a"}] * 2)


def test_an_edited_system_prompt_is_refused(tmp_path):
    (tmp_path / "system_prompt.txt").write_text("policy v2")
    cfg = _cfg(tmp_path).model_copy(update={"system_prompt_hash": sha256_str("policy v1")})
    with pytest.raises(PinMismatch, match="system_prompt_hash"):
        load_system_prompt(cfg)
