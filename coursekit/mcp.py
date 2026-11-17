"""A bounded stdio MCP client for the course's fixed local note server.

The public API cannot choose an executable. This is a teaching subset of MCP,
not a general-purpose client for arbitrary external servers.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time

from .mcp_server import MAX_MESSAGE_BYTES, PROTOCOL_VERSION, strict_json


class MCPError(RuntimeError):
    def __init__(self, code: int, message: str):
        self.code, self.message = code, message
        super().__init__(f"MCP {code}: {message}")


class _RemoteError(MCPError):
    """A well-formed RPC error does not invalidate the transport."""


class LocalMCPClient:
    """Start, initialize, use and close the assigned read-only local server."""
    def __init__(self, workspace, *, trace=None, timeout=10):
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        self.timeout = float(timeout)
        self.trace = trace
        self.process = None
        self._closed = False
        self._next_id = 1
        self._buffer = bytearray()
        self._stderr_bytes = 0
        self._selector = selectors.DefaultSelector()
        server_file = Path(__file__).with_name("mcp_server.py")
        # No shell, no imported user startup configuration, no inherited secrets.
        command = [sys.executable, "-I", "-S", str(server_file), "--workspace", os.path.abspath(workspace)]
        environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        try:
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=environment, bufsize=0, start_new_session=False,
            )
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                os.set_blocking(stream.fileno(), False)
            self._selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
            self._selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
            self._emit("mcp_lifecycle", phase="started", pid=self.process.pid, transport="stdio")
            result = self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "coursekit-client", "version": "1.0.0"},
            })
            capabilities = result.get("capabilities")
            info = result.get("serverInfo")
            if result.get("protocolVersion") != PROTOCOL_VERSION:
                raise MCPError(-32600, "server selected an unsupported protocol version")
            if not isinstance(capabilities, dict) or not isinstance(capabilities.get("tools"), dict):
                raise MCPError(-32600, "server did not advertise tools capability")
            if not isinstance(info, dict) or not all(isinstance(info.get(key), str) for key in ("name", "version")):
                raise MCPError(-32600, "serverInfo is missing")
            self.server_info = info
            self.protocol_version = result["protocolVersion"]
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"}, time.monotonic() + self.timeout)
            self._emit("mcp_lifecycle", phase="initialized", protocol_version=self.protocol_version)
        except BaseException:
            self.close()
            raise

    def _emit(self, event, **data):
        if self.trace is not None:
            self.trace.emit(event, **data)

    def _send(self, message, deadline):
        if self._closed or self.process is None or self.process.poll() is not None:
            raise MCPError(-32000, "server process is closed")
        try:
            payload = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
        except (TypeError, ValueError, UnicodeError) as error:
            raise MCPError(-32602, "request cannot be represented as UTF-8 JSON") from error
        if len(payload) > MAX_MESSAGE_BYTES:
            raise MCPError(-32602, "request exceeds the message size limit")
        with selectors.DefaultSelector() as writable:
            writable.register(self.process.stdin, selectors.EVENT_WRITE)
            offset = 0
            while offset < len(payload):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not writable.select(remaining):
                    raise MCPError(-32001, "request write timed out")
                try:
                    offset += os.write(self.process.stdin.fileno(), payload[offset:])
                except BlockingIOError:
                    continue
                except (BrokenPipeError, OSError) as error:
                    raise MCPError(-32000, "server closed its input") from error
        self._emit("mcp_send", message=message)

    def _receive(self, deadline):
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                if newline + 1 > MAX_MESSAGE_BYTES:
                    raise MCPError(-32600, "response exceeds the message size limit")
                raw = bytes(self._buffer[:newline])
                del self._buffer[:newline + 1]
                try:
                    message = strict_json(raw.decode("utf-8", errors="strict"))
                except (ValueError, UnicodeError, RecursionError) as error:
                    raise MCPError(-32700, "server sent malformed JSON") from error
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise MCPError(-32600, "server sent an invalid JSON-RPC message")
                self._emit("mcp_receive", message=message)
                return message
            if len(self._buffer) >= MAX_MESSAGE_BYTES:
                raise MCPError(-32600, "response exceeds the message size limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPError(-32001, "request timed out")
            ready = self._selector.select(remaining)
            if not ready:
                raise MCPError(-32001, "request timed out")
            for key, _ in ready:
                try:
                    block = os.read(key.fileobj.fileno(), 4096)
                except BlockingIOError:
                    continue
                if not block:
                    if key.data == "stdout":
                        raise MCPError(-32000, "server exited before a complete response")
                    self._selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    self._buffer.extend(block)
                else:
                    self._stderr_bytes += len(block)
                    if self._stderr_bytes > MAX_MESSAGE_BYTES:
                        raise MCPError(-32600, "server exceeded its diagnostic output limit")

    def _request(self, method, params):
        request_id = self._next_id
        self._next_id += 1
        deadline = time.monotonic() + self.timeout
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, deadline)
            # Bound even valid but irrelevant notifications from a faulty server.
            for _ in range(32):
                response = self._receive(deadline)
                if "id" not in response and isinstance(response.get("method"), str):
                    continue
                if type(response.get("id")) is not int or response["id"] != request_id:
                    raise MCPError(-32600, "response id does not match the request")
                if ("result" in response) == ("error" in response) or "method" in response:
                    raise MCPError(-32600, "response must have exactly one result or error")
                if "error" in response:
                    error = response["error"]
                    if not isinstance(error, dict) or isinstance(error.get("code"), bool) or not isinstance(error.get("code"), int) or not isinstance(error.get("message"), str):
                        raise MCPError(-32600, "malformed RPC error")
                    raise _RemoteError(error["code"], error["message"])
                if not isinstance(response["result"], dict):
                    raise MCPError(-32600, "MCP result must be an object")
                return response["result"]
            raise MCPError(-32600, "too many unrelated messages")
        except _RemoteError:
            raise
        except (MCPError, OSError, ValueError) as error:
            self._emit("mcp_lifecycle", phase="error", method=method, error=str(error))
            if isinstance(error, MCPError) and error.code == -32001 and method != "initialize":
                try:
                    self._send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id, "reason": "request timeout"}}, time.monotonic() + min(self.timeout, 0.1))
                except (MCPError, OSError, ValueError):
                    pass
            self.close()
            raise

    def list_tools(self):
        result = self._request("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list) or len(tools) != 1 or result.get("nextCursor") is not None:
            self.close()
            raise MCPError(-32600, "expected the course server's fixed one-tool list")
        tool = tools[0]
        if not isinstance(tool, dict) or tool.get("name") != "read_note" or not isinstance(tool.get("inputSchema"), dict):
            self.close()
            raise MCPError(-32600, "invalid course tool description")
        return tools

    def call_tool(self, name, arguments):
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content")
        if not isinstance(content, list) or not isinstance(result.get("isError"), bool) or not all(isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str) for item in content):
            self.close()
            raise MCPError(-32600, "invalid text tool result")
        return result

    def close(self):
        """Close stdin, then terminate and reap a child that does not exit."""
        if self._closed:
            return
        self._closed = True
        process = self.process
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=min(self.timeout, 0.25))
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            self._emit("mcp_lifecycle", phase="closed", pid=process.pid, returncode=process.returncode)
        self._selector.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
