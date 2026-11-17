"""Copy only synthetic lesson inputs into a fresh results/ workspace."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
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
    shutil.copyfile(FIXTURES / "sum-lines.sh", target / "sum-lines.sh")
    inputs = target / "inputs"
    inputs.mkdir()
    for name in ("normal.txt", "no-final-newline.txt", "empty.txt", "single-no-newline.txt"):
        shutil.copyfile(FIXTURES / name, inputs / name)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

