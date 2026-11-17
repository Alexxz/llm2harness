"""Offline stand checks: real local subprocesses, no neural model or network."""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from coursekit.mcp import LocalMCPClient, MCPError
from coursekit.mcp_server import MAX_NOTE_BYTES, PROTOCOL_VERSION


SERVER = Path(__file__).resolve().parents[1] / "coursekit" / "mcp_server.py"


class Recorder:
    def __init__(self):
        self.events = []

    def emit(self, event, **data):
        self.events.append({"event": event, **data})


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="coursekit-mcp-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "notes.txt").write_text("Синтетическая заметка: apples=7\n", encoding="utf-8")

    def test_real_stdio_negotiation_discovery_call_and_cleanup(self):
        trace = Recorder()
        with LocalMCPClient(self.workspace, trace=trace) as client:
            process = client.process
            self.assertEqual(client.protocol_version, PROTOCOL_VERSION)
            tools = client.list_tools()
            self.assertEqual([tool["name"] for tool in tools], ["read_note"])
            self.assertFalse(tools[0]["inputSchema"]["additionalProperties"])
            result = client.call_tool("read_note", {"path": "notes.txt"})
            self.assertFalse(result["isError"])
            self.assertIn("apples=7", result["content"][0]["text"])
        self.assertIsNotNone(process.poll())
        methods = [event["message"].get("method") for event in trace.events if event["event"] == "mcp_send"]
        self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/list", "tools/call"])
        self.assertEqual(len([event for event in trace.events if event["event"] == "mcp_receive"]), 3)

    def test_invalid_arguments_are_protocol_errors_and_connection_survives(self):
        with LocalMCPClient(self.workspace) as client:
            for arguments in ({}, {"path": 7}, {"path": "notes.txt", "extra": True}, {"path": ""}):
                with self.subTest(arguments=arguments), self.assertRaises(MCPError) as error:
                    client.call_tool("read_note", arguments)
                self.assertEqual(error.exception.code, -32602)
            self.assertFalse(client.call_tool("read_note", {"path": "notes.txt"})["isError"])

    def test_unknown_tool_is_protocol_error(self):
        with LocalMCPClient(self.workspace) as client:
            with self.assertRaises(MCPError) as error:
                client.call_tool("execute_anything", {"path": "notes.txt"})
            self.assertEqual(error.exception.code, -32602)

    def test_missing_note_is_tool_error(self):
        with LocalMCPClient(self.workspace) as client:
            result = client.call_tool("read_note", {"path": "absent.txt"})
            self.assertTrue(result["isError"])
            self.assertIn("note_not_found", result["content"][0]["text"])

    def test_traversal_absolute_hidden_and_symlink_paths_are_denied(self):
        outside = self.root / "outside.txt"
        outside.write_text("synthetic outside sentinel", encoding="utf-8")
        (self.workspace / "linked.txt").symlink_to(outside)
        (self.workspace / "linked-directory").symlink_to(self.root, target_is_directory=True)
        (self.workspace / "inside-link.txt").symlink_to(self.workspace / "notes.txt")
        (self.workspace / ".hidden.txt").write_text("synthetic hidden note", encoding="utf-8")
        attempts = ["../outside.txt", str(outside), "linked.txt", "linked-directory/outside.txt", "inside-link.txt", ".hidden.txt", "a//b", "a\\b", "bad\x00name"]
        with LocalMCPClient(self.workspace) as client:
            for path in attempts:
                with self.subTest(path=path):
                    result = client.call_tool("read_note", {"path": path})
                    self.assertTrue(result["isError"])
                    self.assertNotIn("outside sentinel", result["content"][0]["text"])

    def test_regular_utf8_size_and_hardlink_constraints(self):
        (self.workspace / "large.txt").write_bytes(b"x" * (MAX_NOTE_BYTES + 1))
        (self.workspace / "binary.txt").write_bytes(b"\xff\xfe")
        os.link(self.workspace / "notes.txt", self.workspace / "hard-link.txt")
        os.mkfifo(self.workspace / "pipe.txt")
        with LocalMCPClient(self.workspace) as client:
            for path in ["large.txt", "binary.txt", "hard-link.txt", "pipe.txt"]:
                with self.subTest(path=path):
                    self.assertTrue(client.call_tool("read_note", {"path": path})["isError"])

    def test_nested_note_and_boundary_size_are_readable(self):
        nested = self.workspace / "notes"
        nested.mkdir()
        (nested / "limit.txt").write_bytes(b"x" * MAX_NOTE_BYTES)
        with LocalMCPClient(self.workspace) as client:
            result = client.call_tool("read_note", {"path": "notes/limit.txt"})
            self.assertFalse(result["isError"])
            self.assertEqual(len(result["content"][0]["text"]), MAX_NOTE_BYTES)

    def raw_server(self):
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", str(SERVER), "--workspace", str(self.workspace)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        )
        def cleanup():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        self.addCleanup(cleanup)
        return process

    def raw_exchange(self, process, value):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8") + b"\n"
        process.stdin.write(raw)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(2), "local server did not answer")
        return json.loads(process.stdout.readline())

    @staticmethod
    def initialize(version=PROTOCOL_VERSION):
        return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}

    def test_server_requires_initialize_and_initialized(self):
        process = self.raw_server()
        request = {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}
        self.assertEqual(self.raw_exchange(process, request)["error"]["code"], -32002)
        self.assertIn("result", self.raw_exchange(process, self.initialize()))
        self.assertEqual(self.raw_exchange(process, request)["error"]["code"], -32002)
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.assertEqual(len(self.raw_exchange(process, request)["result"]["tools"]), 1)

    def test_server_negotiates_supported_version_for_unknown_request(self):
        process = self.raw_server()
        result = self.raw_exchange(process, self.initialize("1900-01-01"))["result"]
        self.assertEqual(result["protocolVersion"], PROTOCOL_VERSION)

    def test_server_parse_and_protocol_errors(self):
        process = self.raw_server()
        for raw, expected in [(b"not-json\n", -32700), (b"[]\n", -32600), (b'{"jsonrpc":"2.0","id":true,"method":"ping"}\n', -32600), (b'{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\n', -32700)]:
            with self.subTest(raw=raw):
                self.assertEqual(self.raw_exchange(process, raw)["error"]["code"], expected)
        self.assertIn("result", self.raw_exchange(process, self.initialize()))

    def test_server_bounds_incoming_messages(self):
        process = self.raw_server()
        process.stdin.write(b"x" * 131_073 + b"\n")
        process.stdin.close()
        self.assertEqual(process.wait(timeout=2), 2)

    def fake_process(self, program):
        """Replace the fixed child only in tests; production API has no command option."""
        real_popen = subprocess.Popen
        spawned = []
        def spawn(command, **kwargs):
            child = real_popen([sys.executable, "-I", "-S", "-c", program], **kwargs)
            spawned.append(child)
            return child
        return patch("coursekit.mcp.subprocess.Popen", side_effect=spawn), spawned

    def test_client_rejects_unsupported_server_version_and_reaps_child(self):
        program = "import sys,json\nr=json.loads(sys.stdin.readline())\nprint(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':{'protocolVersion':'1900-01-01','capabilities':{'tools':{}},'serverInfo':{'name':'fake','version':'1'}}}),flush=True)\nfor line in sys.stdin: pass\n"
        mocked, spawned = self.fake_process(program)
        with mocked, self.assertRaises(MCPError) as error:
            LocalMCPClient(self.workspace, timeout=0.3)
        self.assertIn("unsupported protocol", str(error.exception))
        self.assertIsNotNone(spawned[0].poll())

    def test_client_rejects_malformed_response_and_reaps_child(self):
        mocked, spawned = self.fake_process("import sys\nsys.stdin.readline()\nprint('not-json',flush=True)\nfor line in sys.stdin: pass\n")
        with mocked, self.assertRaises(MCPError) as error:
            LocalMCPClient(self.workspace, timeout=0.3)
        self.assertEqual(error.exception.code, -32700)
        self.assertIsNotNone(spawned[0].poll())

    def test_initialization_timeout_reaps_hanging_child(self):
        mocked, spawned = self.fake_process("import time\ntime.sleep(5)\n")
        started = time.monotonic()
        with mocked, self.assertRaises(MCPError) as error:
            LocalMCPClient(self.workspace, timeout=0.15)
        self.assertEqual(error.exception.code, -32001)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNotNone(spawned[0].poll())

    def test_call_timeout_cancels_and_reaps_child(self):
        program = (
            "import sys,json,time\n"
            "r=json.loads(sys.stdin.readline())\n"
            f"print(json.dumps({{'jsonrpc':'2.0','id':r['id'],'result':{{'protocolVersion':{PROTOCOL_VERSION!r},'capabilities':{{'tools':{{}}}},'serverInfo':{{'name':'fake','version':'1'}}}}}}),flush=True)\n"
            "sys.stdin.readline()\n"
            "sys.stdin.readline()\n"
            "time.sleep(5)\n"
        )
        mocked, spawned = self.fake_process(program)
        trace = Recorder()
        with mocked:
            client = LocalMCPClient(self.workspace, trace=trace, timeout=0.15)
            with self.assertRaises(MCPError) as error:
                client.call_tool("read_note", {"path": "notes.txt"})
        self.assertEqual(error.exception.code, -32001)
        self.assertIsNotNone(spawned[0].poll())
        sent = [event["message"] for event in trace.events if event["event"] == "mcp_send"]
        self.assertEqual(sent[-1]["method"], "notifications/cancelled")

    def test_close_is_idempotent_and_calls_after_close_fail(self):
        client = LocalMCPClient(self.workspace)
        client.close()
        client.close()
        with self.assertRaises(MCPError):
            client.list_tools()

    def test_invalid_timeout_rejected_without_child(self):
        for timeout in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                LocalMCPClient(self.workspace, timeout=timeout)


if __name__ == "__main__":
    unittest.main()
