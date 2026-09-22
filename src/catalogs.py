"""Tool catalogs and the agent system prompt, as pinned files on disk.

Training and evaluation must render the same system prompt and the same tools.
Reading both live from tau2 would tie every prompt to whatever tau2 checkout
happens to be installed on that machine, and the Kaggle side does not have
tau2 at all. So they are snapshotted once, into data/catalogs/ (committed), and
pinned by hash in configs/base.yaml:

    data/catalogs/system_prompt.txt    tau2's agent instructions + retail policy
    data/catalogs/catalog-16.json      the 16 native retail tools, in tau2's order

Catalogs 40 and 80 (native tools + plausible distractors, plan §5) are built
by V1-06 into the same format. Every consumer goes through load_system_prompt()
and load_catalog(), which check the pins, so an edited file stops a run.

    python src/catalogs.py snapshot     # re-snapshot from tau2; prints hashes to pin
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import ExperimentConfig
from src.hashing import hash_file, sha256_str

DOMAIN = "retail"
SYSTEM_PROMPT_FILE = "system_prompt.txt"


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
    parser.add_argument("command", choices=["snapshot"])
    parser.add_argument("--catalogs-dir", type=Path, default=Path("data/catalogs"))
    args = parser.parse_args(argv)

    hashes = snapshot(args.catalogs_dir)
    print("[catalogs] snapshot written. Pin these in configs/base.yaml after review:")
    for key, value in hashes.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
