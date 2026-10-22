"""Stage E2: DPO preference pairs from the students' own mistakes (plan X5).

Two halves, split by machine (plan X5, "Split between machines"):

  export (Mac, seconds)  every teacher decision in the SFT set's episodes (the
                         train split, successful harvest episodes, catalog 16),
                         rendered by the shared serializer as a prompts JSONL.
                         The same states SFT trained on, by construction.
  [GPU]                  generate.py maps those prompts to student completions:
                         `make pairs-generate MODEL=qwen15b` on the Mac (MLX),
                         or the Kaggle generate notebook. No API quota.
  build (Mac, pure)      compare each completion with the teacher's action and
                         keep the real disagreements as pairs:
                           prompt   = the serialized state
                           chosen   = the teacher's action (+ <|im_end|>)
                           rejected = the student's completion (+ <|im_end|>)

What counts as a disagreement (review these, they decide what DPO pushes away):
  - Agreement is forced eval's: a reply where the teacher replied, or a tool
    call that matches after §3 normalization (metrics.score_step). Reply text is
    never compared (plan X7: score the decision type only), so reply-vs-reply
    never makes a pair.
  - A tool call that differs in FORM but has the same EFFECT is not a failure
    and never becomes a rejected sample. Every call-vs-call disagreement is
    executed: the teacher's earlier calls are replayed on a fresh copy of the
    tau2 retail DB, then each action runs on its own copy, and the two are
    equivalent when every tool output and the final DB hash match. The count of
    these is the pair-quality number the E4 kill gate asks for.
  - Malformed output, a call where the teacher replied, a reply where the
    teacher called (premature termination), a wrong tool or wrong arguments:
    pairs, each tagged with its failure-taxonomy bucket (src/taxonomy.py).
  - The same (state, rejected text) twice, which sampling produces, is one pair.

The executor is passed in (plan X7's constraint): tests use a fake DB, real runs
use Tau2Runner, which imports tau2 lazily. Nothing here imports tau2 at module
scope, so the pure half stays testable without it.

    python src/make_pairs.py export --config configs/qwen15b.yaml --out data/pairs/prompts-train-c16.jsonl
    python src/make_pairs.py build --config configs/qwen15b.yaml \\
        --completions results/completions-qwen15b-sft-train-c16.jsonl \\
        --adapter madhusiddharths1/tool-router-qwen15b-sft --out data/pairs/qwen15b
"""

from __future__ import annotations

import copy
import json
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.catalogs import load_catalog, load_system_prompt
from src.cli import build_parser, resolve
from src.config import load_config
from src.hashing import hash_file, sha256_str, short
from src.invariants import InvariantViolation, assert_pairs_train_only, check_all
from src.metrics import score_step
from src.prompts import IM_END, parse_completion, render_action, template_hash
from src.runlog import read_records
from src.taxonomy import classify
from src.trajectories import Action, ToolCall

NATIVE_CATALOG = 16

# (prefix calls already made, calls to run) -> (each call's output, DB hash after)
Runner = Callable[[Sequence[ToolCall], Sequence[ToolCall]], tuple[list[str], str]]


# --- the pure half -----------------------------------------------------------


def prior_calls(context: Sequence[Mapping[str, Any]]) -> list[ToolCall]:
    """Every tool call the teacher made before this state, in order."""
    calls = []
    for m in context:
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                calls.append(
                    ToolCall(name=str(c["name"]), arguments=dict(c.get("arguments") or {}))
                )
    return calls


def execution_equivalent(
    context: Sequence[Mapping[str, Any]], a: Action, b: Action, run: Runner
) -> bool:
    """Same outputs and same final DB, each action run on its own replayed copy."""
    if a.kind != "tool_call" or b.kind != "tool_call":
        return False
    prefix = prior_calls(context)
    return run(prefix, a.calls) == run(prefix, b.calls)


@dataclass(frozen=True)
class Verdict:
    outcome: str  # "agree" | "equivalent" | "pair"
    bucket: str | None  # taxonomy bucket when outcome == "pair"


def judge(
    reference: Action,
    completion: str,
    catalog_names: Iterable[str],
    context: Sequence[Mapping[str, Any]],
    run: Runner | None,
) -> Verdict:
    """What one completion at one state amounts to. See the module docstring."""
    parsed = parse_completion(completion)
    bucket = classify(score_step(reference, parsed, catalog_names).as_dict())
    if bucket is None:
        return Verdict("agree", None)
    predicted = parsed.action
    if (
        run is not None
        and predicted is not None
        and execution_equivalent(context, reference, predicted, run)
    ):
        return Verdict("equivalent", None)
    return Verdict("pair", bucket)


def as_target(text: str) -> str:
    """A completion as the model emitted it, up to and including the stop token."""
    return text.split(IM_END, 1)[0] + IM_END


@dataclass(frozen=True)
class State:
    step_id: str
    task_id: str
    rep: int
    index: int
    prompt: str
    context: tuple[Mapping[str, Any], ...]
    reference: Action


def build_pairs(
    states: Sequence[State],
    completions: Iterable[Mapping[str, Any]],
    catalog_names: Iterable[str],
    run: Runner | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pairs plus the counts the manifest and the E4 kill gate need."""
    names = list(catalog_names)
    by_step = {s.step_id: s for s in states}
    outcomes: Counter[str] = Counter()
    buckets: Counter[str] = Counter()
    call_disagreements = 0
    pairs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    n = 0
    for rec in completions:
        state = by_step.get(rec["step_id"])
        if state is None:
            raise ValueError(f"completion for unknown state {rec['step_id']}")
        n += 1
        verdict = judge(state.reference, rec["completion"], names, state.context, run)
        outcomes[verdict.outcome] += 1
        predicted = parse_completion(rec["completion"]).action
        if (
            state.reference.kind == "tool_call"
            and predicted is not None
            and predicted.kind == "tool_call"
            and verdict.outcome != "agree"
        ):
            call_disagreements += 1
        if verdict.outcome != "pair":
            continue
        rejected = as_target(rec["completion"])
        key = (state.step_id, rejected)
        if key in seen:
            outcomes["duplicate"] += 1
            continue
        seen.add(key)
        buckets[str(verdict.bucket)] += 1
        pairs.append(
            {
                "pair_id": f"{state.step_id}:{rec.get('sample', 0)}",
                "step_id": state.step_id,
                "task_id": state.task_id,
                "rep": state.rep,
                "index": state.index,
                "sample": rec.get("sample", 0),
                "bucket": verdict.bucket,
                "reference_kind": state.reference.kind,
                "prompt": state.prompt,
                "chosen": render_action(state.reference) + IM_END,
                "rejected": rejected,
            }
        )
    stats = {
        "n_states": len(states),
        "n_completions": n,
        "outcomes": dict(outcomes),
        "n_pairs": len(pairs),
        "pairs_per_bucket": dict(sorted(buckets.items())),
        # Pair quality (E4 kill gate): of the call-vs-call disagreements, how many
        # were the same effect in another form, and so kept OUT of the pairs.
        "call_disagreements": call_disagreements,
        "equivalent_share_of_call_disagreements": (
            round(outcomes["equivalent"] / call_disagreements, 4) if call_disagreements else None
        ),
    }
    return pairs, stats


# --- the tau2 executor (real runs only) --------------------------------------


class Tau2Runner:
    """Replays calls on a fresh copy of the retail DB. tau2 is imported here,
    never at module scope (test_core_is_light). Retail tasks carry no initial
    state (all 114 have initial_state null), so the shipped DB is the start."""

    def __init__(self) -> None:
        from tau2.domains.retail.data_model import RetailDB
        from tau2.domains.retail.utils import RETAIL_DB_PATH

        self._db = RetailDB.load(RETAIL_DB_PATH)
        self._cache: dict[str, tuple[list[str], str]] = {}

    def __call__(
        self, prefix: Sequence[ToolCall], calls: Sequence[ToolCall]
    ) -> tuple[list[str], str]:
        from tau2.data_model.message import ToolCall as Tau2Call
        from tau2.domains.retail.environment import get_environment

        key = json.dumps(
            {
                "prefix": [[c.name, dict(c.arguments)] for c in prefix],
                "calls": [[c.name, dict(c.arguments)] for c in calls],
            },
            sort_keys=True,
            default=str,
        )
        if key in self._cache:
            return self._cache[key]
        env = get_environment(db=copy.deepcopy(self._db))
        for i, c in enumerate(prefix):
            env.get_response(Tau2Call(id=f"p{i}", name=c.name, arguments=dict(c.arguments)))
        outputs = [
            env.get_response(Tau2Call(id=f"c{i}", name=c.name, arguments=dict(c.arguments))).content
            for i, c in enumerate(calls)
        ]
        result = ([str(o) for o in outputs], str(env.get_db_hash()))
        self._cache[key] = result
        return result


# --- CLI ---------------------------------------------------------------------


def load_states(cfg, harvest_cfg) -> tuple[list[State], list[str], list[str]]:
    """The SFT set's decisions: train split, successful harvest episodes, c16."""
    from src.build_sft import assert_train_only
    from src.harvest import load_task_ids, split_task_ids
    from src.invariants import assert_split_disjoint
    from src.prompts import serialize_state
    from src.trajectories import decision_steps, load_episodes

    train_ids, eval_ids = split_task_ids(
        load_task_ids(), cfg.split.n_train, cfg.split.n_eval, cfg.split.split_seed
    )
    assert_split_disjoint(train_ids, eval_ids)
    wanted = set(train_ids[: cfg.harvest.n_tasks])
    episodes = [
        e
        for e in load_episodes(
            cfg.paths.results_dir / "harvest-harvest.jsonl",
            fingerprint=harvest_cfg.teacher_fingerprint(),
        )
        if e.task_id in wanted
    ]
    assert_train_only(episodes, train_ids, eval_ids)
    system = load_system_prompt(cfg)
    tools = load_catalog(cfg, NATIVE_CATALOG)
    states = [
        State(
            step_id=f"{e.task_id}:{e.rep}:{st.index}",
            task_id=e.task_id,
            rep=e.rep,
            index=st.index,
            prompt=serialize_state(system=system, tools=tools, messages=st.context),
            context=st.context,
            reference=st.action,
        )
        for e in episodes
        for st in decision_steps(e.messages)
    ]
    return states, train_ids, eval_ids


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("E2: train-split states -> student completions -> DPO pairs")
    parser.add_argument("cmd", choices=["export", "build"])
    parser.add_argument("--completions", type=Path, help="build: generate.py output JSONL")
    parser.add_argument("--adapter", help="build: the SFT adapter that produced the completions")
    parser.add_argument(
        "--no-execute",
        action="store_true",
        help="build: skip the execution-equivalence check (counts would overstate failures)",
    )
    args = parser.parse_args(argv)
    if args.out is None:
        parser.error("--out is required (export: a JSONL path; build: a directory)")

    cfg = resolve(args)
    harvest_cfg = load_config(args.config)  # never smoke: which harvest
    print(f"[make_pairs] {', '.join(check_all(cfg))}")
    states, train_ids, _ = load_states(cfg, harvest_cfg)
    assert_pairs_train_only([s.task_id for s in states], train_ids)

    if args.cmd == "export":
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w") as fh:
            for s in states:
                fh.write(
                    json.dumps({"step_id": s.step_id, "prompt": s.prompt}, ensure_ascii=False)
                    + "\n"
                )
        n_tasks = len({s.task_id for s in states})
        print(f"[make_pairs] {len(states)} train states from {n_tasks} tasks -> {args.out}")
        return 0

    if args.completions is None or args.adapter is None:
        parser.error("build needs --completions and --adapter")
    if not args.completions.exists():
        parser.error(f"{args.completions} does not exist; run `make pairs-generate` first")
    tools = load_catalog(cfg, NATIVE_CATALOG)
    run: Runner | None = None if args.no_execute else Tau2Runner()
    pairs, stats = build_pairs(
        states, read_records(args.completions), [t["function"]["name"] for t in tools], run
    )
    assert_pairs_train_only([p["task_id"] for p in pairs], train_ids)

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".smoke" if cfg.smoke else ""
    pairs_path = out_dir / f"pairs{suffix}.jsonl"
    pairs_path.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in pairs))
    meta_path = args.completions.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    manifest = {
        "file": pairs_path.name,
        "file_sha256": hash_file(pairs_path),
        "created": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "smoke": cfg.smoke,
        "teacher_fingerprint": harvest_cfg.teacher_fingerprint(),
        "prompt_template_hash": template_hash(),
        "system_prompt_hash": sha256_str(load_system_prompt(cfg)),
        "catalog_size": NATIVE_CATALOG,
        "catalog_hash": cfg.eval.catalog_hashes[NATIVE_CATALOG],
        "base_model": cfg.model.base_model,
        "adapter": args.adapter,
        "completions_file": args.completions.name,
        "completions_sha256": hash_file(args.completions),
        "sampling": meta.get("sampling"),
        "executed": run is not None,
        # Checked on the Mac above; train_dpo re-checks every pair against this
        # list, because Kaggle has no tau2 to recompute the split from.
        "train_task_ids": sorted(train_ids, key=lambda t: (len(t), t)),
        **stats,
    }
    (out_dir / f"manifest{suffix}.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        f"[make_pairs] {stats['n_pairs']} pairs from {stats['n_completions']} completions -> {pairs_path}"
    )
    print(f"[make_pairs]   outcomes: {stats['outcomes']}")
    print(f"[make_pairs]   per bucket: {stats['pairs_per_bucket']}")
    share = stats["equivalent_share_of_call_disagreements"]
    if share is not None:
        print(
            f"[make_pairs]   {stats['outcomes'].get('equivalent', 0)}/{stats['call_disagreements']} "
            f"call disagreements were execution-equivalent ({share:.1%}) and kept out"
        )
    print(f"[make_pairs] manifest {short(manifest['file_sha256'])} -> {out_dir}")
    if not pairs:
        raise InvariantViolation("no pairs: every completion agreed or was equivalent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
