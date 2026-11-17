"""Package observations for independent review. This script does not grade roles."""

from datetime import datetime, timezone
import difflib
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def read(name):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def jsonl(name):
    path = HERE / name
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def dump(name, value):
    (HERE / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    messages = jsonl("role-messages.jsonl")
    actions = jsonl("teacher-actions.jsonl")
    events = read("events.json")
    cleanup = read("cleanup.json")
    final_state = read("final-state.json")
    original = read("original-case.json")
    validator_attempts = jsonl("validation.jsonl")
    requests = jsonl("http-requests.jsonl")
    terminal_raw = (HERE / "terminal.raw").read_bytes()
    display = terminal_raw.decode("utf-8").replace("\r\n", "\n")
    (HERE / "terminal.txt").write_text(display, encoding="utf-8")
    learner_messages = [item for item in messages if item["role"] == "learner"]
    baseline_messages = [item for item in learner_messages if item["kind"].startswith("baseline-")]
    teacher_messages = [item for item in messages if item["role"] == "teacher"]
    posttest_prompt = next(item for item in messages if item["kind"] == "posttest-question")
    posttest_answer = next(item for item in messages if item["kind"] == "posttest-answer")
    proposed = []
    execution_ledger = []
    for item in learner_messages:
        commands = item.get("parsed", {}).get("commands", [])
        if item["kind"] == "posttest-answer":
            continue
        for command in commands:
            proposed.append(command)
            execution_ledger.append({"phase": item["kind"], "chosen_command": command,
                                     "relay_policy": "known classroom console verb only; verbatim text"})
    expected_relay = b"".join((command if command.endswith("\n") else command + "\n").encode("utf-8") for command in proposed)
    actual_relay = (HERE / "relay-input.raw").read_bytes()
    exact_relay = expected_relay == actual_relay
    for item in execution_ledger:
        item["included_in_exact_relay"] = exact_relay
    advice = [event for event in events if event["event"] == "teacher_advice"]
    rounds = []
    for index, event in enumerate(advice, start=1):
        response_messages = [item for item in learner_messages if item["kind"].startswith(f"learner-round-{index}")]
        rounds.append({"index": index, "advice_event": event,
                       "advice_present_in_terminal_bytes": event["data"]["message"].encode("utf-8") in terminal_raw,
                       "learner_messages": response_messages,
                       "executed_commands": [entry for entry in execution_ledger if entry["phase"].startswith(f"learner-round-{index}")],
                       "terminal_evidence_files": sorted(path.name for path in HERE.glob(f"learner-round-{index}*.txt"))})
    last_observe = [item for item in actions if item["request"]["action"] == "observe"][-1]
    pretest = {"question": "Что доказывает успешное принятие ответа заданной JSON Schema? Нужна ли ещё проверка корректности этого ответа?",
               "raw_responses": baseline_messages,
               "classroom_assertion": next(event for event in events if event["event"] == "student_question")}
    teacher_after_posttest = [item for item in actions if item["timestamp"] >= posttest_prompt["timestamp"]]
    posttest = {"question": posttest_prompt["text"], "raw_answer": posttest_answer["text"],
                "answer": posttest_answer.get("parsed"), "question_timestamp": posttest_prompt["timestamp"],
                "answer_timestamp": posttest_answer["timestamp"], "teacher_last_observation_timestamp": last_observe["timestamp"],
                "teacher_did_not_receive_posttest": not teacher_after_posttest,
                "teacher_api_actions_after_question": teacher_after_posttest,
                "grading": "Not performed by the coordinator. Preserve the answer for independent review."}
    limits = [
        "One isolated assistant role-play with an assigned starting belief; no human learners and no population estimate.",
        "The learner README already explains the distinction, so this trace cannot isolate the causal contribution of teacher instructions.",
        "There is no untreated control, repetition, delayed retention check, or blinded experimental comparison.",
        "The HTTP completion is a fixed synthetic fixture. It cannot establish any model quality, prompt improvement, or weight change.",
        "For each captured response, the runner's shell-Python checker lacked jsonschema; the documented system Python checked the same captured bytes. All failed and corrected attempts are retained. This was coordinator preparation, not learner reasoning or teacher success.",
        "The posttest is one independent answer with no teacher assistance. It has not been scored here.",
        "The learner's semantic comparison is a written/manual comparison. No semantic checker or real-model experiment was executed by the learner.",
        "Learner-written response copies add the terminal multiline editor's final newline. Separate harness raw-response files preserve the exact received response bytes.",
        "The roles share one workspace account; isolation relies on fork_turns=none and explicit restricted reads, not separate operating-system users.",
    ]
    content_criteria = [
        "Distinguish parsing one JSON value, satisfying the lesson03 JSON Schema, and matching the source request.",
        "For the original request, identify the erroneous field, expected and observed values, and the acceptance disposition with its reason. Assess the actual learner wording rather than substituting fixture-output mutation for resolution of the learning difficulty.",
        "Justify structural acceptance from allowed fields, types, enum values, and ranges rather than from meaning.",
        "Derive expected field values independently from the held-out input, including the effect of an explicit denial of urgency, then compare them with the supplied answer.",
        "Describe a content check that reads the source or an independently prepared reference, rather than repeating the schema check.",
        "Keep fixed synthetic responses separate from real model observations, and preserve raw response evidence.",
    ]
    paired_inputs = None
    if len(requests) == 2:
        before, after = [item["request"] for item in requests]
        paired_inputs = {"all_http_fields_except_prompt_equal": {k: v for k, v in before.items() if k != "prompt"} == {k: v for k, v in after.items() if k != "prompt"},
                         "response_content_identical": requests[0]["response_content"] == requests[1]["response_content"],
                         "actual_prompt_diff": "".join(difflib.unified_diff(before["prompt"].splitlines(keepends=True), after["prompt"].splitlines(keepends=True), fromfile="actual-first-request", tofile="actual-second-request"))}
        for name, index in (("baseline-prompt.txt", 0), ("examples-prompt.txt", 1)):
            paired_inputs[name + "_equals_actual_request"] = (HERE / "student-artifacts" / name).read_text() == requests[index]["request"]["prompt"]
    written_response_fidelity = {}
    for name in ("original-response.txt", "examples-response.txt"):
        path = HERE / "student-artifacts" / name
        if path.exists():
            copied = path.read_bytes()
            exact = original["completion_text"].encode("utf-8")
            written_response_fidelity[name] = {"byte_identical_to_http_response": copied == exact,
                                               "equal_with_one_terminal_final_newline": copied == exact + b"\n"}
    observations = {"fixture_source": original["source"], "actual_model_loaded": False,
                    "http_request_count": cleanup["http_request_count"], "teacher_advice_count": len(advice),
                    "learner_command_count": len(proposed), "verbatim_relay_matches_all_chosen_commands": exact_relay,
                    "console_student_command_events": [event for event in events if event["event"] == "student_command"],
                    "schema_sent_to_generation": final_state["schema"], "initial_input": original["input"],
                    "captured_fixture_response": original["completion_text"],
                    "validator_attempts": validator_attempts, "paired_inputs": paired_inputs,
                    "learner_written_response_copy_fidelity": written_response_fidelity,
                    "all_advice_visible_in_terminal": all(item["advice_present_in_terminal_bytes"] for item in rounds)}
    observations["fixed_fixture_output"] = {
        "changed": False,
        "basis": "Both actual HTTP responses were identical: coffee/2/false for the unchanged source request about tea.",
        "expected_behavior": "The fixture is deliberately constant. Changing its output was neither expected nor a completion requirement for the learning difficulty.",
    }
    initial_analysis_events = [event for event in events if event["sequence"] in {26, 51}]
    observations["initial_case_analysis_evidence"] = {
        "classroom_event_sequences": [event["sequence"] for event in initial_analysis_events],
        "actual_classroom_utterances": [event["data"]["message"] for event in initial_analysis_events],
        "field_identified_by_learner": "drink",
        "expected_value_stated_by_learner": "tea",
        "observed_value_stated_by_learner": "coffee",
        "expected_full_reference_stated_by_learner": {"drink": "tea", "cups": 2, "urgent": False},
        "recorded_disposition_wording": "JSON разбирается — да; схеме соответствует — да; содержание верно — нет.",
        "reason_stated_by_learner": "Расходится drink: правило требует tea для чая, ответ содержит coffee.",
        "separate_acceptance_disposition": "The learner did not separately use accept/reject wording; the exact content verdict above and full ask utterances are retained without adding a stronger disposition.",
        "actual_actions": "Read the trusted checker output, wrote a response copy, sent the initial-case analysis through ask, then added examples and repeated the fixture request once.",
        "assessment_status": "Whether these actual utterances resolve the initial learning difficulty is left to the independent judge. No coordinator teacher/learner grade is included.",
    }
    report = {"kind": "single_terminal_teaching_playtest", "lesson": "03", "evidence_source": "synthetic_http_fixture_and_isolated_collaboration_roles",
              "status": "completed" if not cleanup.get("error") else "integration_failed",
              "created_at": datetime.now(timezone.utc).isoformat(), "grading": "No learner or teacher grading performed by the coordinator.",
              "observations": observations, "pretest": pretest, "rounds": rounds, "posttest": posttest,
              "role_initializations": {"teacher": (HERE / "teacher-initialization.md").read_text(),
                                       "learner_private": (HERE / "learner-initialization.md").read_text()},
              "all_raw_role_messages": messages, "all_teacher_api_actions": actions,
              "all_delivered_terminal_payloads": {path.name: path.read_text() for path in sorted(HERE.glob("*.txt")) if path.name.startswith(("baseline-followup-", "learner-round-"))},
              "posttest_delivered_payload": (HERE / "posttest-input.txt").read_text(),
              "teacher_final_messages": teacher_messages,
              "learner_selected_commands_and_actual_relay": execution_ledger,
              "coordinator_preparation": ["Copied fixed lesson03 assets into a temporary course root and froze named document hashes.",
                                          "Created the Session and administratively resumed it before either teaching role existed.",
                                          "Started the synthetic ThreadingHTTPServer and a child StudentConsole on a real PTY.",
                                          "Ran the trusted syntax/schema validator, retained its environment failure, and retried the same bytes with /usr/bin/python3.",
                                          "Relayed learner-selected known console commands verbatim; delivered role outputs without tutoring; closed/reaped owned resources."],
              "coordinator_preparation_events_despite_api_role_label": [event for event in events if event["event"] == "artifact_written" and event["data"].get("path") in {"fixture-notice.txt", "validator.json"}],
              "coordinator_controls": {"setup": read("setup-control.json"), "cleanup": read("coordinator-cleanup-request.json"), "runner_finally": "runner.py also closes the Session in finally before copying final evidence."},
              "api_role_label_limit": "The unchanged Session.write_artifact API labels all writes as student, including trusted harness fixture-notice/validator writes; Session.control labels administrative setup/cleanup as teacher. Attribution here uses the exact chosen-command relay, teacher bridge log, and explicit preparation/cleanup records.",
              "session": {"final_state": final_state, "events": events, "pty_proof": read("pty-proof.json")},
              "documents": {"before": read("document-hashes.json"), "after": read("document-hashes-after.json")},
              "runtime": {"before": read("runtime-hashes-before.json"), "after": read("runtime-hashes-after.json")},
              "cleanup": cleanup, "limitations": limits, "content_criteria_for_independent_review": content_criteria,
              "artifacts": {"raw_terminal": "terminal.raw", "readable_terminal": "terminal.txt", "raw_relay_input": "relay-input.raw",
                            "raw_role_messages": "role-messages.jsonl", "teacher_actions": "teacher-actions.jsonl", "copied_session_data": "session-data/",
                            "learner_written_files": "student-artifacts/", "review_packet": "teacher-review-packet.json"}}
    dump("report.json", report)
    # Review packet deliberately excludes private initialization/card and agent identities.
    packet_pretest = {"question": pretest["question"], "responses": [item["parsed"] for item in baseline_messages],
                      "classroom_assertion": pretest["classroom_assertion"]["data"]["message"]}
    packet_rounds = [{"index": item["index"], "advice": item["advice_event"]["data"]["message"],
                      "advice_event_sequence": item["advice_event"]["sequence"],
                      "advice_visible_in_terminal": item["advice_present_in_terminal_bytes"],
                      "learner_responses": [message["parsed"] for message in item["learner_messages"]],
                      "actual_commands": [entry["chosen_command"] for entry in item["executed_commands"]]} for item in rounds]
    packet_observations = {key: observations[key] for key in ("fixture_source", "actual_model_loaded", "http_request_count", "teacher_advice_count", "learner_command_count", "verbatim_relay_matches_all_chosen_commands", "schema_sent_to_generation", "initial_input", "captured_fixture_response", "all_advice_visible_in_terminal")}
    packet_observations["actual_validator_results"] = [{"exit_code": attempt["exit_code"], "stdout": attempt["stdout"], "scope": attempt["scope"]} for attempt in validator_attempts]
    packet_observations["paired_inputs"] = paired_inputs
    packet_observations["learner_written_response_copy_fidelity"] = written_response_fidelity
    packet_observations["fixed_fixture_output"] = observations["fixed_fixture_output"]
    packet_observations["initial_case_analysis_evidence"] = observations["initial_case_analysis_evidence"]
    dump("teacher-review-packet.json", {"lesson": "03", "evidence_source": "single_synthetic_terminal_role_play", "observations": packet_observations,
                                        "pretest": packet_pretest, "rounds": packet_rounds, "posttest": posttest,
                                        "content_criteria": content_criteria, "limitations": limits,
                                        "review_status": "Unscored; no coordinator verdict included."})
    md = ["# One terminal teaching playtest — synthetic evidence", "", "This is one isolated assistant role-play using the existing classroom, a real PTY, and a local fixed HTTP fixture. No model was loaded and no model quality was measured. No learner/teacher grading was performed by the coordinator.", "",
          f"Lesson: 03. Actual HTTP requests: {cleanup['http_request_count']}. Teacher messages through Session.advise: {len(advice)}. Learner-selected console commands relayed: {len(proposed)}. Exact chosen-command/relay match: {exact_relay}.", "",
          "The fixed fixture returned identical bytes on both calls, as designed. Changing that output was not a completion requirement for the learning difficulty.", "",
          "Initial-case analysis evidence (ask events 26 and 51): the learner named drink, stated expected tea versus received coffee, and gave the disposition ‘JSON разбирается — да; схеме соответствует — да; содержание верно — нет’, citing the tea rule. The learner did not separately use accept/reject wording. The exact utterances and executed commands are preserved; the independent judge decides whether this resolves the initial difficulty.", "",
          "## Observed pretest", "", baseline_messages[0]["parsed"]["reply"], "", baseline_messages[-1]["parsed"]["reply"], "",
          "## Terminal interaction", ""]
    for item in rounds:
        md.extend([f"### Advice {item['index']} (event {item['advice_event']['sequence']})", "", item["advice_event"]["data"]["message"], "", "Learner replies:", ""])
        for response in item["learner_messages"]:
            md.extend([response["parsed"]["reply"], ""])
        md.extend(["Actual learner-selected commands:", "", "```text", *[entry["chosen_command"] for entry in item["executed_commands"]], "```", ""])
    md.extend(["## Independent posttest (unscored)", "", posttest_prompt["text"], "", "```json", posttest_answer["text"], "```", "",
               "The teacher was not shown this question or answer and made no subsequent observation. The raw answer is retained for an independent reviewer.", "",
               "## Preparation and verification", "", "For each captured response, the runner's shell-Python checker failed with `No module named 'jsonschema'`. The documented `/usr/bin/python3 -m coursekit check-json` then checked the exact same captured response and returned `valid=true`, `check=syntax_and_schema_only`, and no errors. All four checker attempts are preserved. The coordinator did not infer meaning from this checker result.", "",
               "The learner actually read the checker artifact, wrote response/prompt copies, edited the prompt, and sent the recorded ask messages. The learner performed written/manual semantic comparisons; no executable semantic checker or real-model experiment was run. Learner-written response copies include the multiline editor's final newline; the separate harness raw-response files preserve exact received bytes.", "",
               f"Console exit: {cleanup['console_exit_code']}; console reaped: {cleanup.get('console_reaped')}; server closed: {cleanup['server_closed']}; server thread alive: {cleanup['server_thread_alive']}; temporary root removed: {cleanup.get('temporary_root_removed')}.", "",
               f"Named documents unchanged during the run: {read('document-hashes.json') == read('document-hashes-after.json')}. Fixed runtime source hashes unchanged: {read('runtime-hashes-before.json') == read('runtime-hashes-after.json')}.", "",
               "## Limits", "", *["- " + limit for limit in limits], "",
               "## Evidence", "", "- [Structured report with raw prompts/messages](report.json)", "- [Independent-review packet; no private card or agent identities](teacher-review-packet.json)", "- [Readable terminal transcript](terminal.txt) and [exact PTY bytes](terminal.raw)", "- [Exact relayed input](relay-input.raw)", "- [Teacher API requests/results](teacher-actions.jsonl)", "- [Session events](events.json) and [final state](final-state.json)", "- [Document hashes](document-hashes.json) and frozen-documents/", "- [Protocol integrity checks](integrity-checks.json)", "- [Cleanup record](cleanup.json)", ""])
    (HERE / "report.md").write_text("\n".join(md), encoding="utf-8")
    inventory = {}
    for path in sorted(HERE.rglob("*")):
        if path.is_file() and path.name != "artifact-hashes.json":
            inventory[str(path.relative_to(HERE))] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
    dump("artifact-hashes.json", inventory)
    print(json.dumps({"status": report["status"], "observations": packet_observations, "cleanup": cleanup}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
