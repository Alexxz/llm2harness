#!/usr/bin/env python3
"""Pure offline decision experiment; no requests, tool execution or sleeps."""

import argparse
import json
from pathlib import Path


class TemporaryFailure(RuntimeError):
    """Synthetic transient failure used only by the injected generator."""


def exercise_sequence(max_attempts):
    """Actually call an injected generator repeatedly, without any HTTP or LLM."""
    scripted = iter(["temporary", "temporary", "success"])
    events = []
    calls = 0

    def generate():
        nonlocal calls
        calls += 1
        outcome = next(scripted)
        if outcome == "temporary":
            raise TemporaryFailure("synthetic temporary generation failure")
        return "synthetic-success"

    for attempt in range(1, max_attempts + 1):
        events.append({"event": "attempt_started", "attempt": attempt})
        try:
            text = generate()
        except TemporaryFailure:
            events.append({"event": "temporary_failure", "attempt": attempt})
            continue
        events.append({"event": "generation_succeeded", "attempt": attempt})
        return {"kind": "offline_fixture", "status": "success", "calls": calls,
                "text": text, "events": events}
    events.append({"event": "budget_exhausted", "attempts_used": calls})
    return {"kind": "offline_fixture", "status": "stop_budget", "calls": calls,
            "events": events}


def classify(case):
    failure = case["failure"]
    if failure == "tool_timeout" and case["effect"] != "none":
        return "reconcile"
    if failure == "policy_denied":
        return "stop_policy"
    if failure == "model_transport":
        return "retry_model" if case.get("temporary") else "stop_transport"
    return {
        "model_output": "repair_json",
        "check_failed": "repair_code",
        "tool_timeout": "retry_read",
    }.get(failure, "stop_unknown")


def decide(case, max_attempts):
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    decision = classify(case)
    if decision in {"retry_model", "repair_json", "repair_code", "retry_read"}:
        if case["attempts_used"] >= max_attempts:
            return "stop_budget"
    return decision


def self_test(cases):
    for case in cases:
        if decide(case, 3) != case["expected"]:
            raise AssertionError(f"wrong decision for {case['id']}")
    by_id = {case["id"]: case for case in cases}
    assert decide(by_id["D"], 1) == "reconcile"
    assert decide(by_id["E"], 1) == "stop_policy"
    for case_id in ["A", "B", "C", "F"]:
        assert decide(by_id[case_id], 1) == "stop_budget"
    assert decide({"failure": "model_transport", "temporary": False,
                   "effect": "none", "attempts_used": 1}, 3) == "stop_transport"
    assert decide({"failure": "unrecognized", "effect": "none",
                   "attempts_used": 1}, 3) == "stop_unknown"
    assert exercise_sequence(3)["status"] == "success"
    assert exercise_sequence(3)["calls"] == 3
    assert exercise_sequence(1)["status"] == "stop_budget"
    assert exercise_sequence(1)["calls"] == 1
    print("offline_fixture: decisions, failed-attempt budget and uncertain effects verified")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path,
                        default=Path(__file__).parent / "fixtures" / "cases.json")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--sequence", action="store_true",
                        help="Actually repeat a synthetic injected generator: temporary, temporary, success")
    parser.add_argument("--score", type=Path, help="Score parsed model JSON against the fixed key")
    args = parser.parse_args()
    if args.max_attempts < 1:
        parser.error("--max-attempts must be positive")
    data = json.loads(args.cases.read_text(encoding="utf-8"))
    if data.get("kind") != "offline_fixture":
        parser.error("input must identify itself as offline_fixture")
    cases = data["cases"]
    if args.self_test:
        self_test(cases)
        return
    if args.sequence:
        print(json.dumps(exercise_sequence(args.max_attempts), ensure_ascii=False, indent=2))
        return
    if args.score:
        answer = json.loads(args.score.read_text(encoding="utf-8"))
        if not isinstance(answer, dict):
            parser.error("--score expects a JSON object, not a completion envelope")
        checks = [{"id": case["id"], "expected": case["expected"],
                   "actual": answer.get(case["id"]),
                   "correct": answer.get(case["id"]) == case["expected"]}
                  for case in cases]
        print(json.dumps({"kind": "model_answer_evaluation", "correct": sum(c["correct"] for c in checks),
                          "total": len(checks), "checks": checks}, ensure_ascii=False, indent=2))
        return
    decisions = [{"id": c["id"], "decision": decide(c, args.max_attempts),
                  "attempts_used": c["attempts_used"], "max_attempts": args.max_attempts}
                 for c in cases]
    print(json.dumps({"kind": "offline_fixture", "decisions": decisions}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
