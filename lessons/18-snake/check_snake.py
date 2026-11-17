#!/usr/bin/python3
"""Independent Snake acceptance; generated Bash executes only in DockerSandbox."""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
MAX_OUTPUT_BYTES = 65536
FIELDS = ("tick", "width", "height", "snake", "direction", "food", "score", "status", "reason")


def load_json(path: Path) -> dict:
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("input JSON exceeds 2 MiB")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def check_frame(frame: dict) -> list[str]:
    errors = []
    if not isinstance(frame, dict):
        return ["frame is not an object"]
    missing = [name for name in FIELDS if name not in frame]
    if missing:
        return ["missing fields: " + ", ".join(missing)]
    for name in ("tick", "width", "height", "score"):
        if type(frame[name]) is not int:
            errors.append(name + " must be an integer")
    if errors:
        return errors
    if not (2 <= frame["width"] <= 80 and 2 <= frame["height"] <= 80):
        errors.append("invalid board dimensions")
    if frame["tick"] < 0 or frame["score"] < 0:
        errors.append("negative tick or score")
    snake = frame["snake"]
    if not isinstance(snake, list) or not snake:
        return errors + ["snake must be a nonempty list"]
    seen = set()
    for cell in snake:
        if not isinstance(cell, list) or len(cell) != 2 or any(type(x) is not int for x in cell):
            errors.append("invalid snake coordinate")
            continue
        x, y = cell
        if not (0 <= x < frame["width"] and 0 <= y < frame["height"]):
            errors.append("snake outside board")
        if (x, y) in seen:
            errors.append("duplicate snake cell")
        seen.add((x, y))
    food = frame["food"]
    if food is not None:
        if not isinstance(food, list) or len(food) != 2 or any(type(x) is not int for x in food):
            errors.append("invalid food coordinate")
        elif not (0 <= food[0] < frame["width"] and 0 <= food[1] < frame["height"]):
            errors.append("food outside board")
        elif tuple(food) in seen:
            errors.append("food overlaps snake")
    if frame["direction"] not in ("U", "D", "L", "R"):
        errors.append("invalid direction")
    reasons = {"running": (None,), "game_over": ("wall", "self"), "quit": ("quit",), "won": ("filled",)}
    status, reason = frame["status"], frame["reason"]
    if not isinstance(status, str) or (reason is not None and not isinstance(reason, str)):
        errors.append("invalid status/reason pair")
    elif status not in reasons or reason not in reasons.get(status, ()):
        errors.append("invalid status/reason pair")
    return errors


def fingerprint(snapshot: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path, data in sorted(snapshot.items()):
        digest.update(path.encode("utf-8") + b"\0")
        digest.update(str(len(data)).encode("ascii") + b"\0" + data)
    return digest.hexdigest()


def run_immutable_case(sandbox, snapshot: dict[str, bytes], script: str) -> dict:
    """Each container gets a fresh copy; exported mutations never alter the source."""
    sys.path.insert(0, str(ROOT))
    from coursekit.policy import WorkspacePolicy
    with tempfile.TemporaryDirectory(prefix="course-snake-check-") as directory:
        case_policy = WorkspacePolicy(Path(directory))
        for path, data in snapshot.items():
            case_policy.write_bytes(path, data)
        try:
            result = sandbox.run(Path(directory), script)
        except (OSError, ValueError, RuntimeError) as exc:
            result = {"exit_code": None, "stdout": "", "stderr": str(exc), "timed_out": False}
        try:
            after = case_policy.snapshot()
            unchanged = after == snapshot
        except (OSError, ValueError):
            unchanged = False
        return {**result, "source_unchanged": unchanged, "source_sha256": fingerprint(snapshot)}


def evaluate(cases: list[dict], runs: dict, *, evidence_type: str) -> dict:
    results = []
    syntax = runs.get("syntax", {})
    syntax_errors = []
    if syntax.get("timed_out"):
        syntax_errors.append("syntax check timed out")
    if syntax.get("output_limit"):
        syntax_errors.append("syntax check output was truncated")
    if syntax.get("exit_code") != 0:
        syntax_errors.append("bash -n failed or did not run")
    if syntax.get("source_unchanged") is not True:
        syntax_errors.append("source mutation or missing immutable-source evidence")
    results.append({"id": "syntax", "passed": not syntax_errors, "errors": syntax_errors})
    for case in cases:
        run = runs.get(case["id"])
        errors = []
        if not isinstance(run, dict):
            results.append({"id": case["id"], "passed": False, "errors": ["missing actual run"]})
            continue
        if run.get("timed_out"):
            errors.append("container execution timed out")
        if run.get("output_limit"):
            errors.append("container output was truncated")
        if run.get("source_unchanged") is not True:
            errors.append("source mutation or missing immutable-source evidence")
        code = run.get("exit_code")
        if type(code) is not int:
            errors.append("missing integer exit_code")
        elif case["expected_exit"] == "zero" and code != 0:
            errors.append("expected exit code 0")
        elif case["expected_exit"] == "nonzero" and code == 0:
            errors.append("invalid input was accepted")
        stdout = run.get("stdout", "")
        stderr = run.get("stderr", "")
        if not isinstance(stdout, str) or len(stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
            errors.append("stdout invalid or exceeds 64 KiB")
            stdout = ""
        if case.get("require_stderr") and (not isinstance(stderr, str) or not stderr.strip()):
            errors.append("expected diagnostic on stderr")
        frames = []
        try:
            if "\x1b" in stdout:
                raise ValueError("ANSI escape in headless stdout")
            frames = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append("stdout is not clean JSONL: " + str(exc))
        expected = case["expected_frames"]
        if len(frames) != len(expected):
            errors.append(f"expected {len(expected)} frames, got {len(frames)}")
        for index, (frame, want) in enumerate(zip(frames, expected)):
            frame_errors = check_frame(frame)
            errors.extend(f"frame {index}: {error}" for error in frame_errors)
            if not frame_errors:
                for field, value in want.items():
                    if frame.get(field) != value:
                        errors.append(f"frame {index}: {field}: expected {value!r}, got {frame.get(field)!r}")
        results.append({
            "id": case["id"], "passed": not errors, "errors": errors,
            "invocation": ["bash", "snake.sh", *case["argv"]],
            "actual": run,
        })
    return {
        "evidence_type": evidence_type,
        "profile_id": "snake-wall-v1",
        "passed": all(item["passed"] for item in results),
        "cases": results,
        "manual_acceptance": "not_run",
        "model_run_provenance": "check_separate_agent_trace",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--workspace", type=Path, help="game directory; execution is container-only")
    mode.add_argument("--fixture-report", type=Path, help="explicitly synthetic report; no execution")
    parser.add_argument("--cases", type=Path, default=HERE / "fixtures/headless_cases.json")
    parser.add_argument("--image", default="llm-course-sandbox:1.0")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        suite = load_json(args.cases)
        cases = suite["cases"]
        if not isinstance(cases, list):
            raise ValueError("cases must be a list")
        if args.fixture_report:
            fixture = load_json(args.fixture_report)
            if fixture.get("evidence_type") != "offline_fixture":
                raise ValueError("fixture mode requires evidence_type=offline_fixture")
            runs = fixture["runs"]
            evidence = "offline_fixture"
        else:
            workspace = args.workspace.resolve(strict=True)
            if not workspace.is_dir():
                raise ValueError("workspace is not a directory")
            if args.output and args.output.resolve().is_relative_to(workspace):
                raise ValueError("acceptance report must be outside the game workspace")
            sys.path.insert(0, str(ROOT))
            from coursekit.sandbox import DockerSandbox
            from coursekit.policy import WorkspacePolicy
            source_policy = WorkspacePolicy(workspace)
            original = source_policy.snapshot()
            sandbox = DockerSandbox(image=args.image, timeout=5)
            runs = {"syntax": run_immutable_case(sandbox, original, "bash -n snake.sh")}
            for case in cases:
                command = shlex.join(["bash", "snake.sh", *case["argv"]])
                runs[case["id"]] = run_immutable_case(sandbox, original, command)
            evidence = "container_execution"
        report = evaluate(cases, runs, evidence_type=evidence)
        if not args.fixture_report:
            report["source_sha256"] = fingerprint(original)
            report["original_workspace_unchanged"] = source_policy.snapshot() == original
            if not report["original_workspace_unchanged"]:
                report["passed"] = False
                report["cases"].append({"id": "source_stability", "passed": False, "errors": ["original workspace changed during acceptance"]})
        text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        print(text, end="")
        return 0 if report["passed"] else 1
    except (OSError, ValueError, KeyError, ImportError, RuntimeError) as exc:
        print("Acceptance could not run: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

