"""Verify W&B and Phoenix are live. Week 0 item: "tracking initialized".

Deliberately does no instrumentation — that lands in week 1 alongside the
harvest, where the acceptance test is "a smoke run appears in both dashboards".
This only answers: is the key valid, and is the collector up.

    make tracking
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

DEFAULT_PHOENIX = "http://localhost:6006"
DEFAULT_PROJECT = "tool-router-ladder"


def check_phoenix() -> bool:
    endpoint = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", DEFAULT_PHOENIX)
    try:
        with urllib.request.urlopen(endpoint, timeout=5) as response:
            ok = response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"  FAIL  phoenix unreachable at {endpoint}\n        {exc}")
        print("        start it with:  make phoenix")
        return False
    print(f"  ok    phoenix serving at {endpoint}")
    return ok


def check_wandb() -> bool:
    if not os.environ.get("WANDB_API_KEY"):
        print("  FAIL  WANDB_API_KEY not set")
        print("        get one at https://wandb.ai/authorize, then put it in .env")
        return False

    import wandb  # heavy, and only needed here

    project = os.environ.get("WANDB_PROJECT", DEFAULT_PROJECT)
    try:
        run = wandb.init(project=project, name="week0-tracking-check", tags=["setup"])
        run.log({"setup/ok": 1})
        url = run.url
        run.finish()
    except Exception as exc:
        print(f"  FAIL  wandb init failed: {exc}")
        return False

    print(f"  ok    wandb project '{project}' reachable")
    print(f"        run: {url}")
    return True


def main() -> int:
    print("Tracking check\n")
    results = [check_phoenix(), check_wandb()]
    if all(results):
        print("\nBoth live. Week 0 tracking item done.")
        return 0
    print("\nNot ready — fix the FAIL lines above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
