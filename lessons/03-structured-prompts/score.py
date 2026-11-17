"""Measure raw JSON, schema and task accuracy on fixed synthetic requests."""

import argparse
import json
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parent


def reject_constant(value):
    raise ValueError(f"non-JSON constant: {value}")


def score(responses, source):
    cases = json.loads((ROOT / "fixtures/evaluation-cases.json").read_text(encoding="utf-8"))
    schema = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    rows = []
    for case in cases:
        path = responses / f"{case['id']}.json"
        row = {"id": case["id"], "available": path.is_file(), "json_valid": False, "schema_valid": False, "content_correct": False}
        if not path.is_file():
            row["error"] = "missing_response"
            rows.append(row)
            continue
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            text = envelope.get("text") if isinstance(envelope, dict) else None
            if not isinstance(text, str):
                raise ValueError("response envelope needs string field text")
            actual = json.loads(text, parse_constant=reject_constant)
            row["json_valid"] = True
            errors = sorted(validator.iter_errors(actual), key=lambda error: str(error.path))
            row["schema_valid"] = not errors
            row["content_correct"] = not errors and actual == case["expected"]
            row["actual"] = actual
            if errors:
                row["error"] = errors[0].message
            elif not row["content_correct"]:
                row["error"] = "content_mismatch"
        except (OSError, ValueError, KeyError) as error:
            row["error"] = str(error)
        rows.append(row)
    totals = {"total_cases": len(cases)}
    for field in ["available", "json_valid", "schema_valid", "content_correct"]:
        totals[field] = sum(row[field] for row in rows)
    return {"source": source, "totals": totals, "cases": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--responses", type=Path, required=True)
    parser.add_argument("--source", choices=["offline_fixture", "live_model"], required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = json.dumps(score(args.responses, args.source), ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(report, encoding="utf-8")
        print(report, end="")
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
