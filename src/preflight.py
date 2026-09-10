"""The pre-launch checklist, automated where a machine can check it.

division-of-labor.md: "Run before any job over an hour. Claude Code can automate
this as `make preflight`. Nine checkboxes against a nine-hour run."

Machine-checkable items fail this command. Items that need your eyes (Kaggle
quota, a real resume test, token validity) are printed as a checklist — they are
not silently marked done.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from src.config import ExperimentConfig, load_config
from src.invariants import (
    InvariantViolation,
    assert_catalog_hash,
    assert_prompt_template_match,
    assert_results_appendable,
    assert_seed_discipline,
    assert_simulator_frozen,
)

CONFIG_DIR = Path("configs")
BASE = "base.yaml"  # not a runnable cell on its own

MANUAL = [
    "Resume path tested — killed a run and restarted it once",
    "HF token valid (write scope) and checkpoint destination reachable",
    "Remaining Kaggle quota exceeds the estimate with 20% headroom",
    "vLLM serves on the target T4, or the HF generate fallback is in place",
]


def experiment_configs() -> list[ExperimentConfig]:
    paths = sorted(p for p in CONFIG_DIR.glob("*.yaml") if p.name != BASE)
    if not paths:
        raise SystemExit(f"no experiment configs found in {CONFIG_DIR}/")
    return [load_config(p) for p in paths]


def _git_is_clean() -> bool | None:
    """None when git is unavailable rather than a misleading False."""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return not out.stdout.strip()


def run() -> int:
    configs = experiment_configs()
    reference = configs[0].simulator
    failures: list[str] = []

    def check(label: str, fn) -> None:
        try:
            fn()
        except InvariantViolation as exc:
            failures.append(label)
            print(f"  FAIL  {label}\n        {exc}")
        else:
            print(f"  ok    {label}")

    print(f"Preflight over {len(configs)} config(s): {', '.join(c.name for c in configs)}\n")

    check(
        "prompt template hash identical across configs",
        lambda: assert_prompt_template_match(*configs),
    )

    for cfg in configs:
        check(
            f"[{cfg.name}] seed set explicitly, enough seeds for a CI",
            lambda c=cfg: assert_seed_discipline(c),
        )
        check(
            f"[{cfg.name}] simulator frozen against '{configs[0].name}'",
            lambda c=cfg: assert_simulator_frozen(c, reference),
        )
        check(
            f"[{cfg.name}] results path exists and appends",
            lambda c=cfg: assert_results_appendable(c.paths.results_dir / f"{c.name}.jsonl"),
        )

    # Catalogs do not exist until week 2; check only what is on disk. Sizes are
    # shared across configs, so report each one once.
    seen: set[int] = set()
    for cfg in configs:
        for size in cfg.eval.catalog_sizes:
            if size in seen:
                continue
            seen.add(size)
            path = cfg.paths.catalogs_dir / f"tools_{size}.json"
            if path.exists():
                check(
                    f"catalog {size} matches pinned hash",
                    lambda c=cfg, s=size, p=path: assert_catalog_hash(c, s, p),
                )
            else:
                print(f"  --    catalog {size} not built yet ({path})")

    clean = _git_is_clean()
    if clean is None:
        print("  --    git unavailable; confirm smoke passed on this commit yourself")
    elif clean:
        print("  ok    working tree clean — smoke ran on this exact commit")
    else:
        print("  WARN  working tree dirty; `make smoke` may not reflect what you launch")

    print("\nCheck these yourself before launching:")
    for item in MANUAL:
        print(f"  [ ] {item}")

    if failures:
        print(f"\nPREFLIGHT FAILED: {len(failures)} check(s). Do not launch.")
        return 1
    print("\nPreflight passed. Manual items above are still yours.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
