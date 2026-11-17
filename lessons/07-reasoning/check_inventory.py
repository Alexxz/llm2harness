"""Independent arithmetic oracle for lesson 07; never executes model text."""

import argparse
import json
from pathlib import Path


def expected(case):
    values = [case["initial"], case["reserved"], *case["arrivals"], *case["sales"], *case["returns"]]
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("Inventory inputs must be nonnegative integers")
    physical = case["initial"] + sum(case["arrivals"]) - sum(case["sales"]) + sum(case["returns"])
    if not 0 <= case["reserved"] <= physical:
        raise ValueError("Reserved stock must fit in physical stock")
    return {"physical": physical, "available": physical - case["reserved"]}


def model_object(value):
    if isinstance(value, dict) and value.get("fixture_kind") == "offline_fixture":
        return value["model_response"]
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return json.loads(value["text"])
    return value


def evaluate(value, target):
    result = model_object(value)
    format_ok = (
        isinstance(result, dict)
        and {"physical", "available"} <= result.keys()
        and result.keys() <= {"physical", "available", "plan"}
        and type(result["physical"]) is int
        and type(result["available"]) is int
        and ("plan" not in result or (isinstance(result["plan"], list) and len(result["plan"]) == 2 and all(isinstance(item, str) for item in result["plan"])))
    )
    correct = format_ok and all(result[key] == value for key, value in target.items())
    return {"format_ok": format_ok, "correct": correct, "expected": target, "observed": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", default="base", choices=["base", "no_reserve", "small"])
    parser.add_argument("--response", type=Path, help="Bare model JSON, completion envelope with text, or labelled offline fixture")
    args = parser.parse_args()
    cases = json.loads((Path(__file__).parent / "fixtures/cases.json").read_text(encoding="utf-8"))["cases"]
    target = expected(cases[args.case])
    if args.response is None:
        print(json.dumps({"case": args.case, "expected": target}, ensure_ascii=False, indent=2))
        return 0
    try:
        value = json.loads(args.response.read_text(encoding="utf-8"))
        result = evaluate(value, target)
    except (ValueError, KeyError, TypeError) as error:
        result = {"format_ok": False, "correct": False, "error": str(error), "expected": target}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["correct"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
