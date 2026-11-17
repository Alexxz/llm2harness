#!/usr/bin/env python3
"""Inspect synthetic recovery states without running a model or tool."""

import argparse
import json
from pathlib import Path


def recovery_decision(state, *, has_answer=False):
    if state.get("schema_version") != 1:
        return "reject_version"
    if state.get("pending") is not None:
        return "outcome_unknown"
    if state["status"] in {"finished", "budget_exhausted"}:
        return "return_saved_result"
    if state["steps"] >= state["max_steps"]:
        return "stop_budget"
    if state["status"] == "waiting_for_user" and not has_answer:
        return "wait_for_answer"
    return "next_model_request"


def self_test(cases):
    for case in cases:
        assert recovery_decision(case["state"]) == case["expected"], case["id"]
    by_id = {case["id"]: case["state"] for case in cases}
    assert recovery_decision(by_id["waiting"], has_answer=True) == "next_model_request"
    assert recovery_decision(by_id["finished"], has_answer=True) == "return_saved_result"
    assert recovery_decision(by_id["pending_write"], has_answer=True) == "outcome_unknown"
    exhausted = {**by_id["completed_read"], "steps": 6}
    assert recovery_decision(exhausted) == "stop_budget"
    print("offline_fixture: pending effects, completion, waiting, version and cumulative budget verified")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--states", type=Path,
                        default=Path(__file__).parent / "fixtures" / "states.json")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--has-answer", action="store_true")
    parser.add_argument("--show-naive-replay", action="store_true")
    args = parser.parse_args()
    document = json.loads(args.states.read_text(encoding="utf-8"))
    if document.get("kind") != "offline_fixture":
        parser.error("expected explicitly labeled offline_fixture")
    cases = document["cases"]
    if args.self_test:
        self_test(cases)
        return
    if args.show_naive_replay:
        # These are integers in memory, not real writes or commands.
        effect_count = 1
        naive_count = effect_count + 1
        safe_count = effect_count
        print(json.dumps({"kind": "offline_fixture", "before_recovery": effect_count,
                          "naive_replay": naive_count, "stop_and_reconcile": safe_count,
                          "explanation": "A lost acknowledgment does not undo a completed effect"}, indent=2))
        return
    print(json.dumps({"kind": "offline_fixture", "decisions": [
        {"id": c["id"], "decision": recovery_decision(c["state"], has_answer=args.has_answer),
         "steps": c["state"]["steps"], "max_steps": c["state"]["max_steps"]}
        for c in cases]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
