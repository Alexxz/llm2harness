"""Render a task with an actual independently obtained container report."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report_path = Path(args.report)
    if report_path.stat().st_size > 262144:
        parser.error("report is too large")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("mode") != "container_oracle" or report.get("status") != "checked":
        parser.error("need an actual completed container oracle report, not an infrastructure failure")
    if not isinstance(report.get("cases"), list) or not report["cases"]:
        parser.error("report has no checked cases")
    target = Path(args.output).absolute()
    boundary = ROOT / "results"
    if boundary.is_symlink() or target != target.resolve() or not target.is_relative_to(boundary):
        parser.error("output must be inside course results/ without symlinks or parent traversal")
    template = (HERE / "prompts" / "variation.txt").read_text(encoding="utf-8")
    rendered = template.replace("{{OBSERVED_REPORT}}", json.dumps(report, ensure_ascii=False, indent=2))
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as handle:
        handle.write(rendered)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

