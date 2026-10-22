"""The SFT checkpoint registry: which adapters DPO and GRPO may branch from.

Plan X4: DPO and GRPO are parallel branches from the same SFT checkpoint, never
stacked. invariants.assert_parent_is_sft checks a parent's weights against this
list, by sha256 of adapter_model.safetensors, so a repo re-pushed with different
weights stops matching.

configs/sft_registry.yaml is reviewed and committed like any config. `add` reads
the hash from the Hub's file metadata (the LFS oid is the file's sha256), so it
downloads nothing:

    python src/registry.py add madhusiddharths1/tool-router-qwen15b-sft
    python src/registry.py list
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.hashing import hash_file, short

REGISTRY = Path("configs/sft_registry.yaml")
WEIGHTS = "adapter_model.safetensors"


def load_registry(path: Path = REGISTRY) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    return {str(repo): dict(entry) for repo, entry in (raw.get("sft") or {}).items()}


def save_registry(entries: Mapping[str, Mapping[str, str]], path: Path = REGISTRY) -> None:
    header = (
        "# SFT checkpoints DPO/GRPO may branch from (plan X4). Written by\n"
        "# `python src/registry.py add <repo>`; sha256 is adapter_model.safetensors.\n"
    )
    body = yaml.safe_dump({"sft": {k: dict(v) for k, v in sorted(entries.items())}}, sort_keys=True)
    path.write_text(header + body)


def adapter_sha256(adapter_dir: str | Path) -> str:
    """The identity of a local adapter: its weights file's sha256."""
    return hash_file(Path(adapter_dir) / WEIGHTS)


def hub_sha256(repo: str, revision: str | None = None) -> str:
    """sha256 of the adapter weights on the Hub, from metadata alone."""
    from huggingface_hub import HfApi

    infos = HfApi().get_paths_info(repo, [WEIGHTS], revision=revision)
    lfs = getattr(infos[0], "lfs", None) if infos else None
    if lfs is None:
        raise SystemExit(f"{repo} has no LFS-tracked {WEIGHTS} at its root")
    return str(lfs.sha256)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SFT checkpoint registry")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="register an SFT adapter repo by its weights' sha256")
    add.add_argument("repo")
    add.add_argument("--revision")
    sub.add_parser("list")
    args = parser.parse_args(argv)

    entries = load_registry()
    if args.cmd == "list":
        for repo, entry in sorted(entries.items()):
            print(f"{repo}  {short(entry['sha256'])}")
        return 0
    sha = hub_sha256(args.repo, args.revision)
    entries[args.repo] = {"sha256": sha, **({"revision": args.revision} if args.revision else {})}
    save_registry(entries)
    print(f"[registry] {args.repo} -> {short(sha)} in {REGISTRY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
