#!/usr/bin/env python3
"""Compare parsed output to the known action for this lesson's report.txt task."""

import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--input", type=Path, required=True)
args = parser.parse_args()
expected = json.loads((Path(__file__).parent / "fixtures/expected.json").read_text(encoding="utf-8"))
try:
    actual = json.loads(args.input.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    parser.exit(1, f"FAIL: ответ не прочитан как JSON: {exc}\n")
if actual != expected:
    parser.exit(1, "FAIL: JSON не выражает ожидаемое чтение report.txt\n")
print("PASS: действие соответствует запросу этого опыта")
