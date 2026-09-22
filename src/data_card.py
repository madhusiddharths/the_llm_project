"""Data card for the harvested training set (plan §6, week 2).

What it answers, from cached files only — no API, no GPU, seconds to run:
  - how many episodes and decisions the rejection-sampled harvest yields,
  - which tools the teacher actually uses (class imbalance, §6),
  - how long a single decision's prompt is, which decides max_seq_len, whether
    the policy fits in the prompt, and what catalog 80 costs in context.

Prompt lengths are measured on the real serializer output (src/prompts.py) with
the pinned system prompt and catalog-16. Token counts use the student's own
tokenizer when it is in the local Hugging Face cache, and characters /
CHARS_PER_TOKEN otherwise; the card says which. Catalogs 40 and 80 are
extrapolated at the mean rendered size of a native tool until V1-06 builds them.

No --smoke flag: the full run reads cached JSON and finishes in seconds, so it
is its own smoke test. The logic is unit-tested in tests/test_data_card.py.

    python src/data_card.py --config configs/qwen05b.yaml
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cli import DEFAULT_CONFIG
from src.config import load_config
from src.hashing import short
from src.prompts import render_tool, serialize_episode, serialize_state
from src.trajectories import Episode, Step, all_steps

# Rough ratio for English prose mixed with JSON under a BPE tokenizer. Only
# used to size decisions; train.py measures the real thing.
CHARS_PER_TOKEN = 3.5
SEQ_LIMITS = (2048, 4096, 8192, 12288, 16384)


def pct(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile. No numpy: this runs in the light environment."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, round(q / 100 * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def summarize(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"n": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 1),
        "p50": pct(values, 50),
        "p95": pct(values, 95),
        "max": float(max(values)),
    }


def estimate_tokens(text: str) -> int:
    return round(len(text) / CHARS_PER_TOKEN)


def _length_stats(tokens: Sequence[int]) -> dict[str, Any]:
    n = max(1, len(tokens))
    return {
        **summarize(tokens),
        **{f"over_{lim}": round(sum(t > lim for t in tokens) / n, 3) for lim in SEQ_LIMITS},
    }


def build_card(
    episodes: Sequence[Episode],
    *,
    train_ids: Sequence[str],
    logged_cells: int,
    tool_schemas: Sequence[Mapping[str, Any]],
    system_prompt: str,
    catalog_sizes: Sequence[int],
    count_tokens: Callable[[str], int] = estimate_tokens,
) -> dict[str, Any]:
    """Every number on the card. Pure: all tau2 access happens in the caller.

    Prompt estimates cover the native catalog plus every configured catalog
    larger than it. A configured catalog SMALLER than the native tool count
    cannot hold every tool the teacher used; it is reported, not estimated.
    """
    steps: list[tuple[Episode, Step]] = all_steps(episodes)
    calls = [s for _, s in steps if s.action.kind == "tool_call"]
    replies = [s for _, s in steps if s.action.kind == "reply"]

    per_task = Counter(e.task_id for e in episodes)
    by_temp = Counter(e.temperature for e in episodes)
    steps_per_ep = Counter(id(e) for e, _ in steps)

    native = sorted(str(t["function"]["name"]) for t in tool_schemas)
    tool_use = Counter(c.name for s in calls for c in s.action.calls)

    results = [m for e in episodes for m in e.messages if m.get("role") == "tool"]

    tools = list(tool_schemas)
    decision_tokens = [
        count_tokens(serialize_state(system=system_prompt, tools=tools, messages=s.context))
        for _, s in steps
    ]
    episode_tokens = [
        count_tokens(
            "".join(
                seg.text
                for seg in serialize_episode(system=system_prompt, tools=tools, messages=e.messages)
            )
        )
        for e in episodes
    ]
    policy_tokens = count_tokens(system_prompt)
    tool_tokens = [count_tokens(render_tool(t)) for t in tools]
    mean_tool = sum(tool_tokens) / max(1, len(tool_tokens))

    n_native = len(tools)
    prompt_variants: dict[str, dict[str, Any]] = {
        f"decision_catalog{n_native}": _length_stats(decision_tokens),
        f"decision_catalog{n_native}_no_policy": _length_stats(
            [t - policy_tokens for t in decision_tokens]
        ),
        f"episode_catalog{n_native}": _length_stats(episode_tokens),
    }
    for size in sorted(s for s in set(catalog_sizes) if s > n_native):
        extra = round(mean_tool * (size - n_native))
        prompt_variants[f"decision_catalog{size}_extrapolated"] = _length_stats(
            [t + extra for t in decision_tokens]
        )

    return {
        "episodes": {
            "logged_cells": logged_cells,
            "successful_episodes": len(episodes),
            "train_tasks": len(train_ids),
            "tasks_with_success": len(per_task),
            "tasks_without_success": sorted(set(train_ids) - set(per_task), key=_task_order),
            "episodes_per_task": dict(Counter(per_task.values())),
            "by_temperature": {
                str(k): v for k, v in sorted(by_temp.items(), key=lambda kv: kv[0] or 0)
            },
        },
        "decisions": {
            "total": len(steps),
            "tool_calls": len(calls),
            "replies": len(replies),
            "parallel_tool_calls": sum(len(s.action.calls) > 1 for s in calls),
            "per_episode": summarize(list(steps_per_ep.values())),
        },
        "tools": {
            "native": native,
            "catalogs_smaller_than_native": sorted(s for s in set(catalog_sizes) if s < n_native),
            "use": {name: tool_use.get(name, 0) for name in native},
            "never_called": [n for n in native if tool_use.get(n, 0) == 0],
            "unknown_called": sorted(set(tool_use) - set(native)),
        },
        "tool_results": {
            "count": len(results),
            "errors": sum(str(m.get("error")) == "True" for m in results),
            "chars": summarize([len(m.get("content") or "") for m in results]),
        },
        "prompt_lengths": {
            "system_prompt_tokens": policy_tokens,
            "native_tools_tokens": sum(tool_tokens),
            "mean_tool_tokens": round(mean_tool),
            "variants": prompt_variants,
        },
    }


def _task_order(tid: str) -> tuple[int, str]:
    return (int(tid), tid) if tid.isdigit() else (1 << 30, tid)


def render_markdown(card: Mapping[str, Any], *, header: Mapping[str, str]) -> str:
    ep, dec, tools, res, pl = (
        card["episodes"],
        card["decisions"],
        card["tools"],
        card["tool_results"],
        card["prompt_lengths"],
    )
    total_calls = max(1, dec["tool_calls"])
    lines = [
        "# Data card — teacher harvest",
        "",
        *(f"- **{k}:** {v}" for k, v in header.items()),
        "",
        "## Episodes",
        "",
        f"- Logged cells: {ep['logged_cells']}; successful (reward 1.0): **{ep['successful_episodes']}**",
        f"- Train tasks with at least one success: **{ep['tasks_with_success']} / {ep['train_tasks']}**",
        f"- Tasks with no successful episode (absent from training): {', '.join(ep['tasks_without_success']) or 'none'}",
        "- Successful episodes per task: "
        + ", ".join(f"{k} → {v} tasks" for k, v in sorted(ep["episodes_per_task"].items())),
        "- By sampling temperature: "
        + ", ".join(f"T={k}: {v}" for k, v in ep["by_temperature"].items()),
        "",
        "## Decisions (greeting excluded)",
        "",
        f"- **{dec['total']}** teacher decisions: **{dec['tool_calls']}** tool calls, **{dec['replies']}** replies to the user",
        f"- Parallel tool calls: {dec['parallel_tool_calls']}",
        f"- Per episode: mean {dec['per_episode']['mean']}, median {dec['per_episode']['p50']:.0f}, max {dec['per_episode']['max']:.0f}",
        "",
        "## Tool frequency (teacher tool calls)",
        "",
        "| Tool | Calls | Share |",
        "|---|---:|---:|",
        *(
            f"| `{name}` | {n} | {n / total_calls:.1%} |"
            for name, n in sorted(tools["use"].items(), key=lambda kv: -kv[1])
        ),
        "",
        f"- Never called: {', '.join(f'`{n}`' for n in tools['never_called']) or 'none'}",
        f"- Called but not in the native catalog: {', '.join(tools['unknown_called']) or 'none'}",
        *(
            [
                f"- **Configured catalog size(s) {tools['catalogs_smaller_than_native']} are smaller than "
                f"the {len(tools['native'])} native tools**, so they cannot contain every tool the teacher used."
            ]
            if tools["catalogs_smaller_than_native"]
            else []
        ),
        "",
        "## Tool results",
        "",
        f"- {res['count']} results, {res['errors']} errors; characters: mean {res['chars']['mean']}, "
        f"p95 {res['chars']['p95']:.0f}, max {res['chars']['max']:.0f}",
        "",
        f"## Prompt lengths ({header.get('Token counts', 'estimated')})",
        "",
        f"- System prompt (tau2 instructions + retail policy): {pl['system_prompt_tokens']} tokens",
        f"- Native tools, compact rendering: {pl['native_tools_tokens']} tokens "
        f"(~{pl['mean_tool_tokens']} per tool)",
        "- `decision_*`: the prompt at one teacher decision (what eval sends).",
        "- `episode_*`: one whole training sequence (what train.py sees).",
        "",
        "| Variant | p50 | p95 | max | > 2048 | > 4096 | > 8192 | > 12288 | > 16384 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        *(
            f"| {k} | {v['p50']:.0f} | {v['p95']:.0f} | {v['max']:.0f} | {v['over_2048']:.0%} | "
            f"{v['over_4096']:.0%} | {v['over_8192']:.0%} | {v['over_12288']:.0%} | {v['over_16384']:.0%} |"
            for k, v in pl["variants"].items()
        ),
        "",
    ]
    return "\n".join(lines)


def local_token_counter(base_model: str) -> Callable[[str], int] | None:
    """The student's tokenizer from the local HF cache, or None. Never downloads."""
    try:
        from transformers import AutoTokenizer

        tok = AutoTokenizer.from_pretrained(base_model, local_files_only=True)
    except Exception:  # not installed, or not cached: fall back to the estimate
        return None
    return lambda text: len(tok(text, add_special_tokens=False)["input_ids"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help="harvest log (default: results/harvest-harvest.jsonl)",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=None, help="default: the config's results_dir"
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    log_path = args.log or cfg.paths.results_dir / "harvest-harvest.jsonl"
    out_dir = args.out_dir or cfg.paths.results_dir

    from src.harvest import load_task_ids, split_task_ids
    from src.invariants import assert_split_disjoint
    from src.runlog import read_records
    from src.trajectories import load_episodes

    train_ids, eval_ids = split_task_ids(
        load_task_ids(), cfg.split.n_train, cfg.split.n_eval, cfg.split.split_seed
    )
    assert_split_disjoint(train_ids, eval_ids)

    fingerprint = cfg.teacher_fingerprint()
    episodes = load_episodes(log_path, fingerprint=fingerprint)
    leaked = sorted({e.task_id for e in episodes} - set(train_ids))
    if leaked:
        raise ValueError(f"harvest episodes outside the train split: {leaked}")

    logged = sum(1 for r in read_records(log_path) if r.get("config_fingerprint") == fingerprint)
    from src.catalogs import load_catalog, load_system_prompt

    schemas, system = load_catalog(cfg, 16), load_system_prompt(cfg)
    counter = local_token_counter(cfg.model.base_model)
    card = build_card(
        episodes,
        train_ids=train_ids,
        logged_cells=logged,
        tool_schemas=schemas,
        system_prompt=system,
        catalog_sizes=cfg.eval.catalog_sizes,
        **({"count_tokens": counter} if counter else {}),
    )

    header = {
        "Harvest log": str(log_path),
        "Teacher": cfg.teacher.model,
        "Teacher fingerprint": short(fingerprint),
        "Generated": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "Token counts": f"{cfg.model.base_model} tokenizer"
        if counter
        else f"estimated at {CHARS_PER_TOKEN} chars/token (tokenizer not cached)",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "data_card.json").write_text(json.dumps({"header": header, **card}, indent=2) + "\n")
    markdown = render_markdown(card, header=header)
    (out_dir / "data_card.md").write_text(markdown)
    print(markdown)
    print(f"[data_card] wrote {out_dir / 'data_card.md'} and data_card.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
