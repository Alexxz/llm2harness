"""Prepare a fresh synthetic workspace beneath this project's results directory."""

import argparse
import json
from pathlib import Path


def prepare(workspace, *, reserved=15, missing_stock=False):
    results_root = Path(__file__).resolve().parents[2] / "results"
    root = results_root.resolve()
    destination = workspace.resolve()
    if destination == root or not destination.is_relative_to(root):
        raise ValueError("Choose a new child directory beneath results/")
    if workspace.is_symlink():
        raise ValueError("Workspace symlinks are not allowed")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Workspace must be empty; choose a new name to preserve earlier evidence")
    source = Path(__file__).parent / "fixtures/stock.json"
    stock = json.loads(source.read_text(encoding="utf-8"))
    if not 0 <= reserved <= 159:
        raise ValueError("reserved must be between 0 and 159 for this fixture")
    stock["reserved"] = reserved
    destination.mkdir(parents=True, exist_ok=True)
    if not missing_stock:
        (destination / "stock.json").write_text(json.dumps(stock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"workspace": str(destination), "stock_present": not missing_stock, "reserved": reserved}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--reserved", type=int, default=15)
    parser.add_argument("--missing-stock", action="store_true")
    args = parser.parse_args()
    try:
        result = prepare(args.workspace, reserved=args.reserved, missing_stock=args.missing_stock)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
