"""Exercise actual policies with scripted inputs; no model or generated Bash is run."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PUBLIC_NOTE = "PUBLIC_INSIDE_NOTE_FOR_POLICY_CHECK"
PUBLIC_CANARY = "PUBLIC_OUTSIDE_CANARY_FOR_POLICY_CHECK"
CASE_DESCRIPTIONS = [
    {"name": "read_inside", "expect": "allowed read returns the synthetic inside note"},
    {"name": "write_inside", "expect": "allowed write creates inside.txt"},
    {"name": "read_parent", "expect": "policy_denied before reading a synthetic sibling file"},
    {"name": "read_absolute", "expect": "policy_denied before reading a synthetic absolute path"},
    {"name": "write_parent", "expect": "policy_denied and no file created outside workspace"},
    {"name": "read_symlink", "expect": "policy_denied before following a synthetic escaping link"},
    {"name": "shell_not_granted", "expect": "policy_denied and no container or host shell started"},
]


def fresh_root(value: str) -> Path:
    candidate = Path(value).absolute()
    boundary = ROOT / "results"
    if boundary.is_symlink() or candidate != candidate.resolve():
        raise ValueError("use a real course results/ subdirectory without symlinks or parent traversal")
    if candidate == boundary or not candidate.is_relative_to(boundary):
        raise ValueError("workspace must be a dedicated course results/ subdirectory")
    if candidate.exists():
        raise ValueError("probe directory must not exist; previous evidence is preserved")
    candidate.mkdir(parents=True)
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace")
    parser.add_argument("--describe", action="store_true")
    args = parser.parse_args()
    if args.describe:
        print(json.dumps({"mode": "offline_fixture_description",
                          "cases": CASE_DESCRIPTIONS}, ensure_ascii=False, indent=2))
        return 0
    if not args.workspace:
        parser.error("--workspace is required unless --describe is used")

    from coursekit.agent import run_agent
    from coursekit.model import ModelClient
    from coursekit.trace import Trace

    class ScriptedClient(ModelClient):
        def __init__(self, action):
            super().__init__()
            self.actions = [action, {"action": "finish",
                                     "arguments": {"answer": "Конец заданной проверки стенда"}}]

        def generate(self, prompt, **kwargs):
            action = self.actions.pop(0)
            return {"text": json.dumps(action, ensure_ascii=False),
                    "metadata": {"source": "offline_fixture", "real_model_called": False}}

    try:
        target = fresh_root(args.workspace)
        reports = []
        for description in CASE_DESCRIPTIONS:
            name = description["name"]
            parent = target / name
            workspace = parent / "work"
            workspace.mkdir(parents=True)
            (workspace / "notes.txt").write_text(PUBLIC_NOTE, encoding="utf-8")
            canary = parent / "outside-canary.txt"
            canary.write_text(PUBLIC_CANARY, encoding="utf-8")
            outside_write = parent / "outside-written.txt"
            if name == "read_inside":
                action = {"action": "read_file", "arguments": {"path": "notes.txt"}}
            elif name == "write_inside":
                action = {"action": "write_file", "arguments": {"path": "inside.txt", "content": "inside"}}
            elif name == "read_parent":
                action = {"action": "read_file", "arguments": {"path": "../outside-canary.txt"}}
            elif name == "read_absolute":
                action = {"action": "read_file", "arguments": {"path": str(canary)}}
            elif name == "write_parent":
                action = {"action": "write_file", "arguments": {"path": "../outside-written.txt", "content": "outside"}}
            elif name == "read_symlink":
                (workspace / "escape.txt").symlink_to(canary)
                action = {"action": "read_file", "arguments": {"path": "escape.txt"}}
            else:
                action = {"action": "run_bash", "arguments": {"script": "printf 'SHELL_MUST_NOT_RUN\\n'"}}

            trace = Trace()
            run = run_agent(ScriptedClient(action), "Выполни учебную проверку разрешённого действия.",
                            workspace, max_steps=2, trace=trace,
                            allowed_tools=("read_file", "write_file"))
            events = run.get("events", trace.events)
            denied = [event for event in events if event.get("event") == "policy_denied"]
            def event_action(event):
                value = event.get("action")
                return value.get("action") if isinstance(value, dict) else value

            successful_results = [event for event in events
                                  if event.get("event") == "tool_result"
                                  and event_action(event) == action["action"]
                                  and event.get("result", {}).get("ok") is True]
            encoded = json.dumps(run, ensure_ascii=False)
            unchanged = canary.read_text(encoding="utf-8") == PUBLIC_CANARY
            if name == "read_inside":
                passed = not denied and bool(successful_results) and PUBLIC_NOTE in encoded
            elif name == "write_inside":
                artifact = workspace / "inside.txt"
                passed = not denied and artifact.is_file() and artifact.read_text(encoding="utf-8") == "inside"
            else:
                passed = bool(denied) and not successful_results and PUBLIC_CANARY not in encoded
            passed = passed and unchanged and not outside_write.exists()
            reports.append({"name": name, "pass": passed, "expected": description["expect"],
                            "outside_file_unchanged": unchanged, "run": run})
        passed = all(report["pass"] for report in reports)
        print(json.dumps({"mode": "offline_fixture", "real_model_called": False,
                          "pass": passed, "cases": reports}, ensure_ascii=False, indent=2))
        return 0 if passed else 1
    except Exception as exc:
        print(json.dumps({"mode": "offline_fixture", "real_model_called": False,
                          "pass": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

