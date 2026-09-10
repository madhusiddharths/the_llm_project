"""Canonical hashing. One implementation, imported everywhere.

A second copy of a hash helper is the same class of bug as a second copy of the
prompt template (plan Red Flags): the two call sites drift, and an invariant
that should abort a run silently passes instead.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SHORT_LEN = 12


def _coerce(obj: Any) -> Any:
    """Make the few non-JSON types we actually use hash stably."""
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(str(x) for x in obj)
    raise TypeError(f"{type(obj).__name__} is not canonically hashable; convert it explicitly")


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace, literal UTF-8.

    ensure_ascii=False matters — otherwise the same string hashes differently
    depending on whether it arrived from YAML or from a JSON round-trip.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_coerce,
    )


def sha256_str(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_obj(obj: Any) -> str:
    """Full sha256 of any JSON-shaped object, key order irrelevant."""
    return sha256_str(canonical_json(obj))


def hash_file(path: str | Path) -> str:
    """Stream a file's bytes. Used for tool catalogs, which can be large."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def short(digest: str) -> str:
    """Truncate for filenames and log lines. Never for comparisons."""
    return digest[:SHORT_LEN]
