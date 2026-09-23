"""Tool catalogs and the agent system prompt, as pinned files on disk.

Training and evaluation must render the same system prompt and the same tools.
Reading both live from tau2 would tie every prompt to whatever tau2 checkout
happens to be installed on that machine, and the Kaggle side does not have
tau2 at all. So they are snapshotted once, into data/catalogs/ (committed), and
pinned by hash in configs/base.yaml:

    data/catalogs/system_prompt.txt    tau2's agent instructions + retail policy
    data/catalogs/catalog-16.json      the 16 native retail tools, in tau2's order

    data/catalogs/catalog-40.json      native + 24 distractors   (build)
    data/catalogs/catalog-80.json      native + 64 distractors   (build)

Every consumer goes through load_system_prompt() and load_catalog(), which
check the pins, so an edited file stops a run.

DISTRACTORS (plan §5; SHARED COMPONENT "distractor generation", reviewed by
Madhu). They are mined from BFCL's function docs (data/raw/bfcl/, Apache-2.0),
not invented, and must be plausible near-neighbours: "random distractors make
the task artificially easy and the result meaningless". Rules:
  - Rank by overlap with the native tools. Shared OBJECT words (order, item,
    user, address, payment, ...) count 3, shared VERB words (get, cancel,
    modify, ...) count 1, and description overlap with native object words
    counts 0.5 per word, capped at 2. Ties break by name.
  - Catalog 40 is a strict subset of catalog 80 (the top 24 of the top 64), so
    any drop from 40 to 80 is attributable to the 40 tools that were added.
  - A distractor never shares a name with a native tool. BFCL has its own
    get_order_details and get_product_details (trading and shopping APIs);
    exact collisions are dropped, not renamed.
  - Names are sanitised to [A-Za-z0-9_-] (OpenAI's function-name rule, which the
    teacher's API enforces); the original name is kept in the provenance.
  - Native tools are NOT kept at the top. Each catalog is a seeded permutation of
    all its tools, so a model cannot learn "the real tools come first".
  - Schemas are normalised to JSON Schema (BFCL writes "dict", "float",
    "tuple"). Descriptions are kept verbatim.

    python src/catalogs.py snapshot     # re-snapshot from tau2; prints hashes to pin
    python src/catalogs.py build        # catalogs 40 and 80 from BFCL; prints hashes
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import ExperimentConfig
from src.hashing import hash_file, sha256_str

DOMAIN = "retail"
SYSTEM_PROMPT_FILE = "system_prompt.txt"
BFCL_DIR = Path("data/raw/bfcl")
CATALOG_SEED = 7  # the split seed; recorded in every built catalog


def catalog_path(catalogs_dir: Path, size: int) -> Path:
    return Path(catalogs_dir) / f"catalog-{size}.json"


# --- live tau2 (snapshot time only) -----------------------------------------


def _quiet_tau2() -> None:
    from loguru import logger

    logger.remove()  # tau2 logs its whole registry at import time


def live_native_tools() -> list[dict[str, Any]]:
    """The retail tools exactly as tau2 hands them to the teacher."""
    _quiet_tau2()
    from tau2.registry import registry

    env = registry.get_env_constructor(DOMAIN)()
    return [t.openai_schema for t in env.get_tools()]


def live_system_prompt() -> str:
    """The system prompt tau2's LLMAgent gave the teacher, byte for byte."""
    _quiet_tau2()
    from tau2.agent.llm_agent import AGENT_INSTRUCTION, SYSTEM_PROMPT
    from tau2.registry import registry

    env = registry.get_env_constructor(DOMAIN)()
    return SYSTEM_PROMPT.format(domain_policy=env.get_policy(), agent_instruction=AGENT_INSTRUCTION)


def write_catalog(path: Path, tools: list[dict[str, Any]], provenance: list[dict[str, str]]) -> str:
    """Write one catalog file and return its sha256.

    No sort_keys: a schema's property order is the argument order the model
    sees, so it is kept as the source gave it.
    """
    names = [t["function"]["name"] for t in tools]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate tool names in catalog: {sorted(names)}")
    if [p["name"] for p in provenance] != names:
        raise ValueError("provenance must list every tool, in catalog order")
    payload = {"size": len(tools), "tools": tools, "provenance": provenance}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return hash_file(path)


def snapshot(catalogs_dir: Path) -> dict[str, str]:
    """Freeze tau2's system prompt and native tools into data/catalogs/."""
    catalogs_dir = Path(catalogs_dir)
    catalogs_dir.mkdir(parents=True, exist_ok=True)
    system = live_system_prompt()
    (catalogs_dir / SYSTEM_PROMPT_FILE).write_text(system)

    tools = live_native_tools()
    provenance = [{"name": t["function"]["name"], "source": f"tau2:{DOMAIN}"} for t in tools]
    native_hash = write_catalog(catalog_path(catalogs_dir, len(tools)), tools, provenance)
    return {
        "system_prompt_hash": sha256_str(system),
        f"catalog_{len(tools)}_hash": native_hash,
    }


# --- distractor catalogs (V1-06) ---------------------------------------------

_BFCL_TYPES = {"dict": "object", "float": "number", "tuple": "array", "int": "integer"}
_KEEP = ("type", "description", "enum", "items", "properties", "required")
# Retail nouns. Deliberately absent, though they occur in native names:
# "detail" and "type" (every API has get_*_details), "agent" (host-agent and
# monitoring APIs), and "return"/"exchange" as nouns (finance: return on
# equity, exchange rates). Those put sculptures and stock returns in the top 24
# of an earlier draft. "return" and "exchange" still count as VERBS.
OBJECT_WORDS = {
    "order", "item", "product", "user", "address", "payment", "email", "zip",
    "customer", "refund", "cart", "account", "shipping", "delivery", "purchase",
    "card", "gift", "human",
}  # fmt: skip
VERB_WORDS = {
    "get", "find", "list", "cancel", "modify", "update", "return", "exchange",
    "transfer", "calculate", "lookup", "check", "set", "search", "place",
}  # fmt: skip


def name_words(text: str) -> set[str]:
    """Lowercase word stems from a name or description: camelCase, _, . and -
    all split, and a trailing plural 's' dropped (orders -> order).

    KNOWN QUIRK, deliberately frozen: the 's' rule also clips words that are
    not plurals ("address" -> "addres", "status" -> "statu"). Comparisons
    between two stemmed names are unaffected, but "address" never matches
    OBJECT_WORDS, so address tools rank lower than intended. Measured
    2026-09-22: a fixed rule would swap 2 of catalog 80's 64 distractors
    (ClientAddress_set_address and geocode_address in; users_setPresence and
    walmart_vegan_products out) and none of catalog 40's. It is NOT fixed,
    because catalogs 40/80 are pinned and the catalog-80 teacher baseline was
    already running on them. `build` must keep reproducing the pinned files.
    """
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    words = re.split(r"[^A-Za-z0-9]+", spaced.lower())
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words if w}


def normalize_schema(prop: Any) -> Any:
    """BFCL's parameter dialect -> JSON Schema, keeping only what the model sees."""
    if not isinstance(prop, Mapping):
        return prop
    out: dict[str, Any] = {}
    for key in _KEEP:
        if key not in prop:
            continue
        value = prop[key]
        if key == "type":
            kind = str(value).lower()
            if kind == "any":
                continue
            value = _BFCL_TYPES.get(kind, kind)
        elif key == "items":
            value = normalize_schema(value)
        elif key == "properties":
            value = {k: normalize_schema(v) for k, v in value.items()}
        out[key] = value
    return out


def sanitize_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:64]


def load_bfcl_functions(raw_dir: Path = BFCL_DIR) -> list[dict[str, Any]]:
    """Every distinct function doc in the downloaded BFCL files, first seen wins.

    Returns OpenAI-shaped tools, each with a private "_source" and
    "_original_name" that build_catalogs() moves into the provenance.
    """
    seen: dict[str, dict[str, Any]] = {}
    files = sorted(Path(raw_dir).glob("*.json")) + sorted(
        Path(raw_dir).glob("multi_turn_func_doc/*.json")
    )
    for path in files:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            docs = record.get("function", [record] if "name" in record else [])
            for fn in docs:
                name = sanitize_name(fn["name"])
                if name in seen:
                    continue
                seen[name] = {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": fn.get("description", ""),
                        "parameters": normalize_schema(fn.get("parameters") or {"type": "object"}),
                    },
                    "_source": f"bfcl:{path.relative_to(raw_dir)}",
                    "_original_name": fn["name"],
                }
    return list(seen.values())


def neighbor_score(tool: Mapping[str, Any], native: Sequence[Mapping[str, Any]]) -> float:
    """How plausible a distractor is next to the native tools. See module doc."""
    native_names = set().union(*(name_words(t["function"]["name"]) for t in native))
    native_desc = set().union(*(name_words(t["function"].get("description", "")) for t in native))
    name = name_words(tool["function"]["name"])
    desc = name_words(tool["function"].get("description", ""))
    objects = len(name & native_names & OBJECT_WORDS)
    verbs = len(name & native_names & VERB_WORDS)
    context = min(2.0, 0.5 * len(desc & native_desc & OBJECT_WORDS))
    return 3.0 * objects + verbs + context


def rank_distractors(
    native: Sequence[Mapping[str, Any]], candidates: Iterable[Mapping[str, Any]]
) -> list[tuple[float, dict[str, Any]]]:
    taken = {t["function"]["name"].lower() for t in native}
    scored = [
        (neighbor_score(c, native), dict(c))
        for c in candidates
        if c["function"]["name"].lower() not in taken
    ]
    scored.sort(key=lambda sc: (-sc[0], sc[1]["function"]["name"]))
    return scored


def build_catalogs(
    native: Sequence[Mapping[str, Any]],
    candidates: Iterable[Mapping[str, Any]],
    sizes: Sequence[int],
    seed: int = CATALOG_SEED,
) -> dict[int, tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    """size -> (tools, provenance) for every size above the native count.

    Nested by construction: each catalog takes the top (size - native) ranked
    distractors, so smaller catalogs are subsets of larger ones.
    """
    ranked = rank_distractors(native, candidates)
    out: dict[int, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    for size in sorted(sizes):
        extra = size - len(native)
        if extra <= 0:
            continue
        if extra > len(ranked):
            raise ValueError(f"catalog {size} needs {extra} distractors, only {len(ranked)} exist")
        entries = [
            (
                {k: v for k, v in t.items() if not k.startswith("_")},
                {"name": t["function"]["name"], "source": f"tau2:{DOMAIN}"},
            )
            for t in native
        ] + [
            (
                {k: v for k, v in t.items() if not k.startswith("_")},
                {
                    "name": t["function"]["name"],
                    "source": t["_source"],
                    "original_name": t["_original_name"],
                    "neighbor_score": score,
                },
            )
            for score, t in ranked[:extra]
        ]
        random.Random(f"{seed}:{size}").shuffle(entries)
        out[size] = ([e[0] for e in entries], [e[1] for e in entries])
    return out


# --- pinned files (every run) ------------------------------------------------


class PinMismatch(RuntimeError):
    """A pinned file on disk does not match its hash in the config."""


def load_system_prompt(cfg: ExperimentConfig) -> str:
    path = cfg.paths.catalogs_dir / SYSTEM_PROMPT_FILE
    if cfg.system_prompt_hash is None:
        raise PinMismatch("system_prompt_hash is not pinned in the config")
    text = path.read_text()
    if sha256_str(text) != cfg.system_prompt_hash:
        raise PinMismatch(f"{path} does not match system_prompt_hash; re-pin only after review")
    return text


def load_catalog(cfg: ExperimentConfig, size: int) -> list[dict[str, Any]]:
    path = catalog_path(cfg.paths.catalogs_dir, size)
    pinned = cfg.eval.catalog_hashes.get(size)
    if pinned is None:
        raise PinMismatch(f"catalog {size} is not pinned in eval.catalog_hashes")
    if hash_file(path) != pinned:
        raise PinMismatch(f"{path} does not match its pinned hash; re-pin only after review")
    tools = json.loads(path.read_text())["tools"]
    if len(tools) != size:
        raise PinMismatch(f"{path} holds {len(tools)} tools, not {size}")
    return tools


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tool catalogs and the system prompt")
    parser.add_argument("command", choices=["snapshot", "build"])
    parser.add_argument("--catalogs-dir", type=Path, default=Path("data/catalogs"))
    parser.add_argument("--sizes", type=int, nargs="+", default=[40, 80])
    args = parser.parse_args(argv)

    if args.command == "snapshot":
        hashes = snapshot(args.catalogs_dir)
    else:
        native = json.loads(catalog_path(args.catalogs_dir, 16).read_text())["tools"]
        built = build_catalogs(native, load_bfcl_functions(), args.sizes)
        hashes = {}
        for size, (tools, provenance) in built.items():
            hashes[f"catalog_{size}_hash"] = write_catalog(
                catalog_path(args.catalogs_dir, size), tools, provenance
            )
            added = [p for p in provenance if p["source"] != f"tau2:{DOMAIN}"]
            print(
                f"[catalogs] catalog {size}: {len(added)} distractors, lowest score "
                f"{min(p['neighbor_score'] for p in added)}"
            )
    print("[catalogs] written. Pin these in configs/base.yaml after review:")
    for key, value in hashes.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
