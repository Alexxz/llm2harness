"""Prepare synthetic note inputs for the live policy comparison."""
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--variant", choices=("baseline", "injection"), required=True)
    args = parser.parse_args()
    target = Path(args.workspace).absolute()
    boundary = ROOT / "results"
    if boundary.is_symlink() or target != target.resolve():
        parser.error("use a real course results/ subdirectory without symlinks or parent traversal")
    if target == boundary or not target.is_relative_to(boundary):
        parser.error("workspace must be a dedicated course results/ subdirectory")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        parser.error("workspace must be absent or empty; existing files are preserved")
    target.mkdir(parents=True, exist_ok=True)
    source = HERE / "fixtures" / ("notes-baseline.txt" if args.variant == "baseline" else "notes-injection.txt")
    (target / "notes.txt").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

