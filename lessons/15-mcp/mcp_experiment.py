#!/usr/bin/env python3
"""Exercise the actual local MCP process with synthetic data and no LLM."""

import argparse
import json
from pathlib import Path
import sys
import tempfile

# Allow running this fixed course exercise from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coursekit.mcp import LocalMCPClient, MCPError
from coursekit.trace import Trace


def exercise():
    trace = Trace()
    checks = []
    with tempfile.TemporaryDirectory(prefix="course-mcp-lesson-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        note = "Synthetic MCP note: mint-472\n"
        (workspace / "notes.txt").write_text(note, encoding="utf-8")
        (root / "outside.txt").write_text("Synthetic outside marker\n", encoding="utf-8")
        with LocalMCPClient(workspace, trace=trace, timeout=5) as client:
            tools = client.list_tools()
            names = [tool["name"] for tool in tools]
            assert names == ["read_note"], names
            assert "inputSchema" in tools[0]
            checks.append({"name": "discovery_with_schema", "passed": True})
            result = client.call_tool("read_note", {"path": "notes.txt"})
            assert result.get("isError") is False, result
            assert result["content"][0]["text"] == note, result
            checks.append({"name": "actual_read", "passed": True})

            for label, name, arguments in [
                ("extra_argument", "read_note", {"path": "notes.txt", "extra": True}),
                ("unknown_tool", "not_a_course_tool", {"path": "notes.txt"}),
            ]:
                try:
                    client.call_tool(name, arguments)
                except MCPError as error:
                    assert error.code == -32602, error
                    checks.append({"name": label, "passed": True, "error_code": error.code})
                else:
                    raise AssertionError(f"{label} was accepted")

            for label, path in [("outside_workspace", "../outside.txt"),
                                ("missing_note", "missing.txt")]:
                result = client.call_tool("read_note", {"path": path})
                assert result.get("isError") is True, result
                joined = "".join(part.get("text", "") for part in result.get("content", []))
                assert "Synthetic outside marker" not in joined
                checks.append({"name": label, "passed": True})

            result = client.call_tool("read_note", {"path": "notes.txt"})
            assert result.get("isError") is False, result
            checks.append({"name": "usable_after_errors", "passed": True})
            process = client.process

        assert process.poll() is not None, "MCP process survived context manager exit"
        checks.append({"name": "server_closed", "passed": True})

    sent = [event["message"] for event in trace.events if event["event"] == "mcp_send"]
    methods = [message.get("method") for message in sent]
    assert methods[:3] == ["initialize", "notifications/initialized", "tools/list"], methods
    assert "id" not in sent[1], "initialized is a notification"
    checks.append({"name": "real_lifecycle_order", "passed": True})
    return {"kind": "offline_fixture", "description": "Actual local MCP transport; synthetic files; no model requests",
            "checks": checks, "events": trace.events}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true",
                        help="Run discovery, read, denial and child cleanup checks")
    parser.parse_args()
    print(json.dumps(exercise(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
