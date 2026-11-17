"""Meaningful preparation checks; no model, Docker or generated Bash executes."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location("lesson11_" + name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PreparationTests(unittest.TestCase):
    def test_eof_inputs_preserve_the_defect_boundary(self):
        fixtures = HERE / "fixtures"
        normal = (fixtures / "normal.txt").read_bytes()
        without = (fixtures / "no-final-newline.txt").read_bytes()
        self.assertEqual(normal, without + b"\n")
        self.assertEqual((fixtures / "empty.txt").read_bytes(), b"")
        self.assertEqual((fixtures / "single-no-newline.txt").read_bytes(), b"7")

    def test_prepare_preserves_existing_experiment(self):
        prepare = load("prepare")
        with tempfile.TemporaryDirectory(prefix="course-lesson11-") as directory:
            prepare.ROOT = Path(directory)
            target = Path(directory) / "results" / "trial"
            argv = ["prepare.py", "--workspace", str(target)]
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(prepare.main(), 0)
            original = (target / "sum-lines.sh").read_bytes()
            with patch.object(sys, "argv", argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as outcome:
                    prepare.main()
            self.assertEqual(outcome.exception.code, 2)
            self.assertEqual((target / "sum-lines.sh").read_bytes(), original)
            self.assertEqual((target / "inputs" / "no-final-newline.txt").read_bytes(), b"1\n2\n3")

    def test_feedback_rejects_infrastructure_failure(self):
        build = load("build_task")
        with tempfile.TemporaryDirectory(prefix="course-lesson11-") as directory:
            build.ROOT = Path(directory)
            report = Path(directory) / "report.json"
            report.write_text(json.dumps({"mode": "container_oracle", "status": "execution_unavailable"}))
            output = Path(directory) / "results" / "task.txt"
            argv = ["build_task.py", "--report", str(report), "--output", str(output)]
            with patch.object(sys, "argv", argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    build.main()
            self.assertFalse(output.exists())

    def test_feedback_rejects_offline_fixture_as_execution(self):
        build = load("build_task")
        with tempfile.TemporaryDirectory(prefix="course-lesson11-") as directory:
            build.ROOT = Path(directory)
            report = Path(directory) / "report.json"
            report.write_text(json.dumps({"mode": "offline_fixture", "status": "checked", "cases": [1]}))
            output = Path(directory) / "results" / "task.txt"
            argv = ["build_task.py", "--report", str(report), "--output", str(output)]
            with patch.object(sys, "argv", argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    build.main()
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
