"""Shared CLI surface. Every script in src/ uses this; none rolls its own.

division-of-labor.md: "Every script gets a --smoke flag. Same code path, tiny
inputs, finishes in under two minutes." The override set lives in
config.SMOKE_OVERRIDES and is applied at load time, so --smoke changes the
inputs and never the logic — there is no `if smoke:` branch anywhere in src/.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from src.config import ExperimentConfig, load_config

DEFAULT_CONFIG = "configs/qwen05b.yaml"


def build_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"experiment config YAML (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="tiny inputs, same code path, finishes in under two minutes",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="override the config seed (must be explicit; never defaulted)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output path (each script documents its own default)",
    )
    return parser


def resolve(args: argparse.Namespace) -> ExperimentConfig:
    """The only path from CLI args to a validated config."""
    return load_config(args.config, smoke=args.smoke, seed=args.seed)
