"""Offline checks of lesson 07–09 helpers, without a model or generated code."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile


sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[2]
LESSONS = PROJECT / "lessons"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def invoke(path, *arguments):
    return subprocess.run([sys.executable, str(path), *map(str, arguments)], capture_output=True, text=True, timeout=10, check=False)


def main():
    inventory = load("lesson07_inventory", LESSONS / "07-reasoning/check_inventory.py")
    report = load("lesson09_report", LESSONS / "09-agent-loop/check_report.py")
    prepare = load("lesson09_prepare", LESSONS / "09-agent-loop/prepare_workspace.py")
    fixtures07 = LESSONS / "07-reasoning/fixtures"
    fixture_cases = json.loads((fixtures07 / "cases.json").read_text(encoding="utf-8"))["cases"]
    assert inventory.expected(fixture_cases["base"]) == {"physical": 159, "available": 144}
    assert inventory.expected(fixture_cases["small"]) == {"physical": 10, "available": 6}
    good = invoke(LESSONS / "07-reasoning/check_inventory.py", "--response", fixtures07 / "correct.json")
    assert good.returncode == 0 and json.loads(good.stdout)["correct"] is True
    bad = invoke(LESSONS / "07-reasoning/check_inventory.py", "--response", fixtures07 / "plausible_wrong.json")
    assert bad.returncode == 1
    bad_result = json.loads(bad.stdout)
    assert bad_result["format_ok"] is True and bad_result["correct"] is False

    source08 = LESSONS / "08-first-tool/fixtures"
    results_root = PROJECT / "results"
    results_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lesson-helpers-check-", dir=results_root) as temporary:
        scratch = Path(temporary)
        accepted = invoke(LESSONS / "08-first-tool/manual_tool.py", "--proposal", source08 / "read_proposal.json")
        assert accepted.returncode == 0 and json.loads(accepted.stdout)["result"]["record_id"] == "synthetic-stock-08"
        denied = invoke(LESSONS / "08-first-tool/manual_tool.py", "--proposal", source08 / "denied_proposal.json", "--source", scratch / "does-not-exist.json")
        assert denied.returncode == 1 and json.loads(denied.stdout)["error"] == "policy_denied"
        observation = scratch / "observation.json"
        observation.write_text(accepted.stdout, encoding="utf-8")
        feedback = scratch / "feedback.txt"
        constructed = invoke(LESSONS / "08-first-tool/make_feedback.py", "--observation", observation, "--output", feedback)
        assert constructed.returncode == 0 and "synthetic-stock-08" in feedback.read_text(encoding="utf-8")

        workspace = scratch / "work"
        prepare.prepare(workspace)
        assert report.check(workspace)["correct"] is False  # A completion claim cannot replace a file.
        (workspace / "report.json").write_text('{"physical":159,"available":144}', encoding="utf-8")
        assert report.check(workspace)["correct"] is True
        try:
            prepare.prepare(workspace)
        except ValueError:
            pass
        else:
            raise AssertionError("Preparation must not overwrite a prior experiment")
        stock = json.loads((workspace / "stock.json").read_text(encoding="utf-8"))
        stock["reserved"] = 24
        (workspace / "stock.json").write_text(json.dumps(stock), encoding="utf-8")
        assert report.check(workspace)["correct"] is False  # Old evidence must fail after input changes.
        (workspace / "report.json").write_text('{"physical":159,"available":135}', encoding="utf-8")
        assert report.check(workspace)["correct"] is True
        missing = scratch / "missing"
        prepare.prepare(missing, missing_stock=True)
        assert report.check(missing, expect_missing=True)["correct"] is True
        try:
            prepare.prepare(PROJECT / "lessons" / "unexpected-workspace")
        except ValueError:
            pass
        else:
            raise AssertionError("Preparation must remain beneath results/")

    assert (LESSONS / "09-agent-loop/prompts/baseline.txt").read_bytes() == (LESSONS / "09-agent-loop/prompts/variation.txt").read_bytes()
    print(json.dumps({"mode": "offline_fixture", "helpers": ["07 arithmetic oracle", "08 read-only manual dispatch", "08 feedback builder", "09 workspace preparation", "09 independent artifact check"], "result": "passed", "model_requests": 0, "agent_loop_executed": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
