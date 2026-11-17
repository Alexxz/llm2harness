#!/usr/bin/env python3
"""These are synthetic stand inputs, not recorded model answers."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("course_training_data", ROOT / "tools/make_training_data.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

fixture = json.loads((Path(__file__).parent / "fixtures/metric-cases.json").read_text(encoding="utf-8"))
for case in fixture["cases"]:
    scores = module.score_text(case["output"], fixture["expected"])
    assert scores == case["expected_metrics"], (case["id"], scores)
    print(json.dumps({"source": fixture["source"], "case": case["id"], **scores}, ensure_ascii=False))
print("PASS: форма и смысл измеряются раздельно; это проверка стенда без модели")
