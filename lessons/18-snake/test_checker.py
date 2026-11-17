"""Tests of the independent oracle, without generated code, model or Docker."""
import copy
import importlib.util
import json
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("snake_acceptance_checker", HERE / "check_snake.py")
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.cases = json.loads((HERE / "fixtures/headless_cases.json").read_text())["cases"]
        self.runs = json.loads((HERE / "fixtures/synthetic-good-report.json").read_text())["runs"]

    def report(self, runs=None):
        return CHECKER.evaluate(self.cases, self.runs if runs is None else runs, evidence_type="offline_fixture")

    def test_explicit_fixture_passes_but_does_not_claim_manual_or_live_acceptance(self):
        report = self.report()
        self.assertTrue(report["passed"])
        self.assertEqual(report["evidence_type"], "offline_fixture")
        self.assertEqual(report["manual_acceptance"], "not_run")
        self.assertEqual(report["model_run_provenance"], "check_separate_agent_trace")

    def test_incorrect_score_fails_even_with_valid_json(self):
        frames = [json.loads(line) for line in self.runs["growth"]["stdout"].splitlines()]
        frames[-1]["score"] = 0
        self.runs["growth"]["stdout"] = "\n".join(json.dumps(frame) for frame in frames)
        report = self.report()
        growth = next(case for case in report["cases"] if case["id"] == "growth")
        self.assertFalse(growth["passed"])
        self.assertTrue(any("score" in error for error in growth["errors"]))

    def test_tail_cell_collision_bug_is_detected(self):
        frames = [json.loads(line) for line in self.runs["tail_freed"]["stdout"].splitlines()]
        frames[-1]["status"] = "game_over"
        frames[-1]["reason"] = "self"
        self.runs["tail_freed"]["stdout"] = "\n".join(json.dumps(frame) for frame in frames)
        self.assertFalse(self.report()["passed"])

    def test_constant_output_cannot_satisfy_distinct_cases(self):
        self.runs["wall"] = copy.deepcopy(self.runs["move"])
        self.assertFalse(self.report()["passed"])

    def test_missing_real_execution_fails(self):
        del self.runs["growth"]
        self.assertFalse(self.report()["passed"])

    def test_timeout_fails_even_if_the_saved_output_looks_correct(self):
        self.runs["growth"]["timed_out"] = True
        self.assertFalse(self.report()["passed"])

    def test_truncation_fails_even_if_the_retained_output_looks_correct(self):
        self.runs["growth"]["output_limit"] = True
        self.assertFalse(self.report()["passed"])

    def test_ansi_stdout_is_rejected(self):
        self.runs["move"]["stdout"] = "\x1b[2J" + self.runs["move"]["stdout"]
        self.assertFalse(self.report()["passed"])

    def test_boolean_score_is_not_an_integer_score(self):
        frame = json.loads(self.runs["growth"]["stdout"].splitlines()[-1])
        frame["score"] = True
        self.assertIn("score must be an integer", CHECKER.check_frame(frame))

    def test_untrusted_status_type_is_rejected_without_crashing(self):
        frame = json.loads(self.runs["move"]["stdout"].splitlines()[-1])
        frame["status"] = []
        self.assertIn("invalid status/reason pair", CHECKER.check_frame(frame))

    def test_untrusted_reason_type_is_rejected_without_crashing(self):
        frame = json.loads(self.runs["move"]["stdout"].splitlines()[-1])
        frame["reason"] = {}
        self.assertIn("invalid status/reason pair", CHECKER.check_frame(frame))

    def test_mutation_during_a_case_fails_acceptance(self):
        self.runs["growth"]["source_unchanged"] = False
        report = self.report()
        growth = next(case for case in report["cases"] if case["id"] == "growth")
        self.assertFalse(growth["passed"])

    def test_each_case_gets_fresh_source_even_if_previous_case_mutated_it(self):
        class MutatingSandbox:
            def __init__(self):
                self.inputs = []

            def run(self, workspace, script):
                self.inputs.append((workspace / "snake.sh").read_bytes())
                (workspace / "snake.sh").write_bytes(b"mutated by synthetic fixture")
                return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

        sandbox = MutatingSandbox()
        original = {"snake.sh": b"original fixture bytes; no program is executed"}
        first = CHECKER.run_immutable_case(sandbox, original, "fixture-only")
        second = CHECKER.run_immutable_case(sandbox, original, "fixture-only")
        self.assertEqual(sandbox.inputs, [original["snake.sh"], original["snake.sh"]])
        self.assertFalse(first["source_unchanged"])
        self.assertFalse(second["source_unchanged"])
        self.assertEqual(first["source_sha256"], second["source_sha256"])


if __name__ == "__main__":
    unittest.main()

