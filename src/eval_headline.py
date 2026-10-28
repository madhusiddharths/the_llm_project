"""Stage I (plan X9, I-1): the free-running headline cells, one quota night at a time.

    make eval-headline                 # run the cells in order until the day's budget is spent
    make eval-headline ARGS=--plan     # checks and progress only; no quota, no model
    make eval-headline ARGS=--smoke    # the first cell under eval_free --smoke (a few requests)

The cells are spec.methods x spec.catalogs (configs/v3/headline.yaml), in the
spec's order. Each cell is one eval_free run of that method's adapter on the
Mac (MLX); this module picks the cell, checks it and launches it. How an
episode runs is eval_free's, unchanged.

The pinned harness
------------------
The plan pins Stage I to v1's simulator and judge, read from the v1 run
records rather than the live config. v1's simulator lost its only provider on
2026-10-20 (Log 2026-10-22), so the reference is the simulator epoch the
headline's teacher baseline was recorded under, pinned the same way:
spec.harness is a teacher fingerprint (teacher, split, simulator, judge) and
must be both
  (a) what the cell config computes now, so an edit to the simulator, judge or
      teacher config fails here, before any quota is spent; and
  (b) the identity of non-smoke records in results/harvest-baseline*.jsonl, so
      the headline never runs against a user no teacher faced.
Every cell uses the one spec config, so the simulator and judge are the same
across methods by construction; the pin is what holds them still across nights.

One process per cell
--------------------
throttle.install wraps litellm once per process, so a second cell in the same
process would budget against the first cell's limiter. Each cell therefore
runs as its own eval_free process. A cell still incomplete after its process
exits 0 means the day's budget ran out (or failed episodes were left unlogged
to retry), so the run stops there and the next night resumes it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import ExperimentConfig, load_config
from src.eval_free import assert_same_harness_as_baseline, cell_identity, cell_tag
from src.hashing import hash_obj, short
from src.invariants import InvariantViolation, check_all
from src.runlog import RunLog

DEFAULT_SPEC = "configs/v3/headline.yaml"


@dataclass(frozen=True)
class Method:
    name: str
    adapter: str  # Hub repo id


@dataclass(frozen=True)
class HeadlineSpec:
    config: str
    harness: str
    catalogs: list[int]
    methods: list[Method]
    n_seeds: int


@dataclass(frozen=True)
class Cell:
    method: str
    catalog: int
    adapter: str
    tag: str
    log: RunLog
    n_tasks: int
    n_seeds: int

    @property
    def total(self) -> int:
        return self.n_tasks * self.n_seeds

    def done(self) -> int:
        """Episodes logged under this cell's identity within its first n_seeds."""
        return sum(1 for _, rep in self.log.completed_cells() if rep < self.n_seeds)

    @property
    def complete(self) -> bool:
        return self.done() >= self.total


def load_spec(path: str | Path) -> HeadlineSpec:
    raw = yaml.safe_load(Path(path).read_text())
    methods = [Method(m["name"], m["adapter"]) for m in raw["methods"]]
    names = [m.name for m in methods]
    if len(set(names)) != len(names):
        raise ValueError(f"{path}: method names repeat: {names}")
    return HeadlineSpec(
        config=raw["config"],
        harness=raw["harness"],
        catalogs=[int(c) for c in raw["catalogs"]],
        methods=methods,
        n_seeds=int(raw["n_seeds"]),
    )


def pinned_harness(cfg: ExperimentConfig, spec: HeadlineSpec) -> str:
    """(a) and (b) of the module docstring. Returns the pinned fingerprint."""
    live = cfg.teacher_fingerprint()
    if live != spec.harness:
        raise InvariantViolation(
            f"{spec.config} computes teacher fingerprint {short(live)}, but the headline is "
            f"pinned to {short(spec.harness)}. The teacher, split, simulator or judge config "
            "changed; Stage I cells must face the user the pinned baseline faced."
        )
    return assert_same_harness_as_baseline(cfg)


def build_cells(
    cfg: ExperimentConfig, spec: HeadlineSpec, harness: str, backend: str
) -> list[Cell]:
    """Every cell, in run order, keyed exactly as eval_free will key it."""
    for c in spec.catalogs:
        if c not in cfg.eval.catalog_sizes:
            raise InvariantViolation(f"catalog {c} not in {cfg.eval.catalog_sizes}")
    if spec.n_seeds < 1 or (not cfg.smoke and spec.n_seeds > len(cfg.eval.seeds)):
        raise InvariantViolation(f"n_seeds {spec.n_seeds} outside 1..{len(cfg.eval.seeds)}")
    n_seeds = min(spec.n_seeds, len(cfg.eval.seeds))  # smoke configs have fewer seeds
    suffix = ".smoke" if cfg.smoke else ""
    cells = []
    for m in spec.methods:
        for c in spec.catalogs:
            # f"{name}-sft" is also eval_free's default tag: the SFT cells share
            # `make free-mlx`'s files.
            tag = cell_tag(cfg, m.adapter, backend, f"{cfg.name}-{m.name}", None)
            identity = cell_identity(cfg, harness, c, m.adapter, backend, None)
            log = RunLog(
                cfg.paths.results_dir / f"free-{tag}-c{c}{suffix}.jsonl",
                config_fingerprint=hash_obj(identity),
                config_name=f"free:{tag}:c{c}",
                smoke=cfg.smoke,
            )
            cells.append(Cell(m.name, c, m.adapter, tag, log, cfg.eval.n_tasks, n_seeds))
    return cells


def eval_free_command(
    spec: HeadlineSpec,
    cell: Cell,
    *,
    backend: str,
    concurrency: int,
    hub_sync: str | None,
    smoke: bool,
) -> list[str]:
    cmd = [
        sys.executable,
        "src/eval_free.py",
        "--config",
        spec.config,
        "--catalog",
        str(cell.catalog),
        "--adapter",
        cell.adapter,
        "--label",
        cell.tag,
        "--n-seeds",
        str(cell.n_seeds),
        "--backend",
        backend,
        "--concurrency",
        str(concurrency),
    ]
    if hub_sync:
        cmd += ["--hub-sync", hub_sync]
    if smoke:
        cmd.append("--smoke")
    return cmd


def adapter_on_hub(repo: str) -> bool:
    from huggingface_hub import repo_exists

    return repo_exists(repo)


def run_cells(
    cells: Sequence[Cell],
    launch: Callable[[Cell], int],
    adapter_exists: Callable[[str], bool],
) -> int:
    """Run the first incomplete cells in order until one is left incomplete."""
    for cell in cells:
        if cell.complete:
            continue
        if not adapter_exists(cell.adapter):
            print(
                f"[eval_headline] stopping before {cell.method} c{cell.catalog}: "
                f"{cell.adapter} is not on the Hub yet."
            )
            return 0
        print(f"\n[eval_headline] ===== {cell.method} c{cell.catalog}: {cell.done()}/{cell.total}")
        code = launch(cell)
        if code != 0:
            print(f"[eval_headline] eval_free exited {code}; stopping.")
            return code
        if not cell.complete:
            print(
                f"[eval_headline] {cell.method} c{cell.catalog} at {cell.done()}/{cell.total} "
                "(budget spent, or failed episodes left to retry); resume next quota day."
            )
            return 0
    print("\n[eval_headline] every headline cell is complete.")
    return 0


def print_plan(cells: Sequence[Cell], adapter_exists: Callable[[str], bool]) -> None:
    print(f"{'cell':<10} {'done':>9}  {'adapter on Hub':<15} results file")
    for cell in cells:
        on_hub = "yes" if adapter_exists(cell.adapter) else "NOT YET"
        print(
            f"{cell.method + ' c' + str(cell.catalog):<10} {cell.done():>4}/{cell.total:<4} "
            f"{on_hub:<15} {cell.log.path}"
        )
    left = sum(c.total - min(c.done(), c.total) for c in cells)
    print(f"\n{left} episodes left over {len(cells)} cells.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage I: the free-running headline cells")
    parser.add_argument("--spec", default=DEFAULT_SPEC)
    parser.add_argument("--plan", action="store_true", help="checks and progress; runs nothing")
    parser.add_argument("--smoke", action="store_true", help="the first cell, eval_free --smoke")
    parser.add_argument(
        "--backend", choices=["mlx", "fixed"], default="mlx", help="fixed: plumbing check"
    )
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--hub-sync", metavar="REPO", help="passed through to eval_free")
    args = parser.parse_args(argv)

    spec = load_spec(args.spec)
    cfg = load_config(spec.config, smoke=args.smoke)
    print(f"[eval_headline] {', '.join(check_all(cfg))}")
    # The pin is checked against the non-smoke config: --smoke shortens the
    # simulator, as in eval_free.
    harness = pinned_harness(load_config(spec.config), spec)
    print(f"[eval_headline] harness pinned: {short(harness)} ({cfg.simulator.model})")
    cells = build_cells(cfg, spec, harness, args.backend)

    if args.plan:
        print_plan(cells, adapter_on_hub)
        return 0
    if args.smoke:
        cells = cells[:1]

    def launch(cell: Cell) -> int:
        cmd = eval_free_command(
            spec,
            cell,
            backend=args.backend,
            concurrency=args.concurrency,
            hub_sync=args.hub_sync,
            smoke=args.smoke,
        )
        return subprocess.run(cmd, check=False).returncode

    # The fixed backend loads no adapter, so a missing one does not matter.
    exists = adapter_on_hub if args.backend == "mlx" else (lambda _repo: True)
    return run_cells(cells, launch, exists)


if __name__ == "__main__":
    raise SystemExit(main())
