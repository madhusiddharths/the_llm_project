"""Pull Kaggle generation output from the Hub into local results/.

`/kaggle/working` is discarded when a session stops, so the generation notebook
pushes its completions to a private HF dataset repo. This is the other half:
fetch them, drop them in results/, and check they are whole before anything
scores them.

Every catalog replays the same 142 baseline episodes, so a complete completions
file is 1,416 lines whatever the catalog size. Short means the run did not
finish; eval_forced would refuse it anyway (a missing step would inflate
agreement on exactly the hard cases), so it is better to hear about it here.

Safe to run repeatedly: each session's commit starts from an empty working
directory and uploads only its own runs, so earlier files stay on the Hub and
re-pulling them is a no-op.

    python src/pull_results.py                    # everything under completions/
    python src/pull_results.py --pattern '*c40*'  # just this session's runs
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXPECTED_LINES = 1416  # 142 episodes x their steps; identical at every catalog


def pull(repo: str, dest: Path, pattern: str, prefix: str = "completions") -> list[Path]:
    from huggingface_hub import snapshot_download

    local = Path(snapshot_download(repo, repo_type="dataset", allow_patterns=f"{prefix}/*"))
    src = local / prefix
    if not src.is_dir():
        raise FileNotFoundError(f"{repo} has no {prefix}/ folder yet")

    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for f in sorted(src.glob(pattern)):
        target = dest / f.name
        unchanged = target.exists() and target.stat().st_size == f.stat().st_size
        shutil.copy(f, target)
        copied.append(target)
        print(f"  {'=' if unchanged else '+'} {f.name} ({f.stat().st_size:,} bytes)")
    return copied


def check(paths: list[Path]) -> int:
    """Non-zero if any completions file is short. Returns the number of bad files."""
    bad = 0
    for p in sorted(paths):
        if p.suffix != ".jsonl":
            continue
        n = sum(1 for line in p.open() if line.strip())
        flag = "" if n == EXPECTED_LINES else f"  <-- SHORT, expected {EXPECTED_LINES}"
        print(f"  {n:>5} {p.name}{flag}")
        bad += n != EXPECTED_LINES
    return bad


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hub completions -> local results/")
    parser.add_argument("--repo", default="madhusiddharths1/tool-router-results")
    parser.add_argument("--dest", type=Path, default=Path("results"))
    parser.add_argument("--pattern", default="*", help="glob over the Hub folder, e.g. '*c40*'")
    args = parser.parse_args(argv)

    print(f"[pull] {args.repo} -> {args.dest}/")
    copied = pull(args.repo, args.dest, args.pattern)
    if not copied:
        print(f"[pull] nothing matched {args.pattern!r}")
        return 1

    print(f"\n[pull] line counts ({len(copied)} files copied):")
    bad = check(copied)
    if bad:
        print(f"\n[pull] {bad} file(s) incomplete — re-run those cells; generate.py resumes.")
        return 1
    print("\n[pull] all complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
