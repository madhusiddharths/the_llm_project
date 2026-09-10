"""The core modules must not import torch.

CI installs requirements-dev.txt (four packages, seconds) rather than the ~1.7 GB
local set. That only holds while config/hashing/prompts/invariants/runlog/cli
stay pure-Python and the heavy imports live inside function bodies.
"""

from __future__ import annotations

import builtins
import importlib
import sys

import pytest

CORE = ["src.hashing", "src.prompts", "src.config", "src.invariants", "src.runlog", "src.cli"]
HEAVY = {"torch", "transformers", "datasets", "peft", "accelerate", "vllm", "wandb", "phoenix"}


@pytest.mark.parametrize("module", CORE)
def test_core_module_imports_without_heavy_deps(module, monkeypatch):
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in HEAVY:
            raise AssertionError(f"{module} imported {name} at module scope; keep it lazy")
        return real_import(name, *args, **kwargs)

    for name in [*CORE, module]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(builtins, "__import__", guarded)
    importlib.import_module(module)


def test_the_guard_actually_catches_a_heavy_import(monkeypatch):
    """Guard the guard: a test that can never fail is worse than no test.

    Uses a stdlib stand-in rather than a real heavy dep. CI installs only
    requirements-dev.txt, so importing torch here would raise ModuleNotFoundError
    instead of tripping the guard, and this test would fail for the wrong reason.
    """
    real_import = builtins.__import__
    sentinel = "textwrap"  # always present; stands in for a heavy dependency

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] == sentinel:
            raise AssertionError("caught")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    with pytest.raises(AssertionError, match="caught"):
        exec("import textwrap")  # compiles to IMPORT_NAME — the path we guard
