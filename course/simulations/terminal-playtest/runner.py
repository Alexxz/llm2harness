"""One bounded teaching playtest; all HTTP completion data here is SYNTHETIC.

Uses the unchanged classroom and a genuine child PTY. No model is loaded.
Only lesson assets/session data live in the temporary course root.
"""

from datetime import datetime, timezone
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time


SOURCE = Path(__file__).resolve().parents[3]
ARTIFACTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SOURCE))
from coursekit.classroom import Session

SOURCE_TEXT = "Две чашки чая, обычный заказ"
FIXED_RESPONSE = '{"drink":"coffee","cups":2,"urgent":false}'
DOCS = [
    "course/METHODOLOGY.md",
    "course/TERMINAL_CLASSROOM.md",
    "lessons/03-structured-prompts/INSTRUCTOR.md",
    "lessons/03-structured-prompts/README.md",
    "lessons/03-structured-prompts/schema.json",
    "lessons/03-structured-prompts/prompts/baseline.txt",
    "lessons/03-structured-prompts/prompts/variation.txt",
]


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append(path, value):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


class FixtureHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        record = {"timestamp": now(), "source": "synthetic_http_fixture", "path": self.path, "request": payload,
                  "response_content": FIXED_RESPONSE}
        self.requests.append(record)
        append(ARTIFACTS / "http-requests.jsonl", record)
        body = json.dumps({"content": FIXED_RESPONSE, "model": "SYNTHETIC_FIXED_FIXTURE_NO_MODEL", "tokens_predicted": 0}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    frozen = ARTIFACTS / "frozen-documents"
    hashes = {}
    for relative in DOCS:
        data = (SOURCE / relative).read_bytes()
        target = frozen / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        hashes[relative] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    dump(ARTIFACTS / "document-hashes.json", hashes)
    runtime_files = ["coursekit/classroom.py", "coursekit/classroom_console.py", "coursekit/classroom_worker.py", "coursekit/model.py", "coursekit/model_http_worker.py", "coursekit/__main__.py"]
    runtime_hashes = {name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() for name in runtime_files}
    dump(ARTIFACTS / "runtime-hashes-before.json", runtime_hashes)

    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    process = None
    master = None
    session = None
    previous_terminal = None
    completed_runs = set()
    started = time.monotonic()
    cleanup = {"started_at": now(), "source": "synthetic_http_fixture", "console_exit_code": None}
    with tempfile.TemporaryDirectory(prefix="course-terminal-teaching-") as temporary:
        root = Path(temporary)
        try:
            lesson = root / "lessons/03-structured-prompts"
            (lesson / "prompts").mkdir(parents=True)
            dump(lesson / "lesson.json", {"id": "03", "experiments": []})
            for variant in ("baseline", "variation"):
                original = (SOURCE / f"lessons/03-structured-prompts/prompts/{variant}.txt").read_text(encoding="utf-8")
                prompt = original.replace("Текущая заявка: Нужно 3 чашки кофе, срочно.", "Текущая заявка: " + SOURCE_TEXT)
                if prompt == original:
                    raise RuntimeError("Expected named lesson03 source request was not found")
                (lesson / f"prompts/{variant}.txt").write_text(prompt, encoding="utf-8")
            shutil.copyfile(SOURCE / "lessons/03-structured-prompts/schema.json", lesson / "schema.json")
            endpoint = f"http://127.0.0.1:{server.server_port}"
            session = Session.create("03", root=root, endpoint=endpoint)
            # Administrative setup only, before either teaching role exists.
            setup_control = session.control("resume")
            dump(ARTIFACTS / "setup-control.json", {"actor": "test_harness_administrative_setup", "receipt": setup_control})
            dump(ARTIFACTS / "context.json", {"source_root": str(SOURCE), "temporary_root": str(root), "session_id": session.id,
                  "endpoint": endpoint, "synthetic": True, "maximum_teacher_advice": 3})
            dump(ARTIFACTS / "original-case.json", {"source": "synthetic_http_fixture", "input": SOURCE_TEXT,
                  "completion_text": FIXED_RESPONSE, "prompt": session.snapshot()["prompt"], "actual_model_loaded": False})
            session.write_artifact("fixture-notice.txt", "SYNTHETIC HTTP FIXTURE, NO MODEL. Responses are fixed by the test harness. They are not measurements of model quality. After a completed request, validator.json records the trusted syntax/schema-only checker on the raw response.\n")
            master, slave = pty.openpty()
            code = "from pathlib import Path; from coursekit.classroom_console import main; import sys; raise SystemExit(main(['student',sys.argv[1]],root=Path(sys.argv[2])))"
            process = subprocess.Popen([sys.executable, "-c", code, session.id, str(root)], cwd=SOURCE,
                                       stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave)
            if os.isatty(0):
                previous_terminal = termios.tcgetattr(0)
                mode = termios.tcgetattr(0)
                mode[3] &= ~termios.ECHO
                termios.tcsetattr(0, termios.TCSANOW, mode)
            dump(ARTIFACTS / "pty-proof.json", {"outer_stdin_isatty": os.isatty(0), "child_master_isatty": os.isatty(master),
                  "console_pid": process.pid, "runner_pid": os.getpid(), "entrypoint": "unchanged coursekit.classroom_console.main -> StudentConsole.run -> Session.claim_owner"})
            with (ARTIFACTS / "terminal.raw").open("wb") as transcript, (ARTIFACTS / "relay-input.raw").open("wb") as relay:
                banner = b"SYNTHETIC LOCAL HTTP FIXTURE; NO REAL MODEL. The schema checker is a separate trusted harness check, available after run as read validator.json.\r\n"
                os.write(1, banner)
                transcript.write(banner)
                transcript.flush()
                while process.poll() is None:
                    if time.monotonic() - started > 1500:
                        raise TimeoutError("Bounded playtest exceeded 25 minutes")
                    readers = select.select([master, 0], [], [], 0.1)[0]
                    if 0 in readers:
                        data = os.read(0, 65536)
                        if not data:
                            break
                        relay.write(data)
                        relay.flush()
                        os.write(master, data)
                    if master in readers:
                        try:
                            data = os.read(master, 65536)
                        except OSError as error:
                            if error.errno != errno.EIO:
                                raise
                            data = b""
                        if data:
                            transcript.write(data)
                            transcript.flush()
                            os.write(1, data)
                    state = session.snapshot()
                    last = state.get("last_run")
                    if last and last["id"] not in completed_runs and last["kind"] == "complete":
                        completed_runs.add(last["id"])
                        result = last.get("result", {})
                        text = result.get("result", {}).get("text")
                        if text is None:
                            raise RuntimeError("Expected unchanged classroom result result.text")
                        response_file = ARTIFACTS / (last["id"] + "-raw-response.txt")
                        response_file.write_text(text, encoding="utf-8")
                        command = [sys.executable, "-m", "coursekit", "check-json", "--schema", str(lesson / "schema.json"), "--input", str(response_file)]
                        checked = subprocess.run(command, cwd=SOURCE, text=True, capture_output=True, timeout=10)
                        validation = {"source": "trusted_test_harness_on_synthetic_http_response", "run_id": last["id"],
                                      "command": command, "exit_code": checked.returncode, "stdout": checked.stdout,
                                      "stderr": checked.stderr, "response_sha256": hashlib.sha256(text.encode()).hexdigest(),
                                      "scope": "syntax_and_schema_only; no semantic result inferred"}
                        append(ARTIFACTS / "validation.jsonl", validation)
                        session.write_artifact("validator.json", json.dumps(validation, ensure_ascii=False, indent=2) + "\n")
                        if len(completed_runs) >= 3:
                            session.control("pause")
                # Drain terminal output that arrived just before console exit.
                deadline = time.monotonic() + 0.3
                while time.monotonic() < deadline and select.select([master], [], [], 0.03)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        break
                    if not data:
                        break
                    transcript.write(data)
                    os.write(1, data)
                transcript.flush()
        except BaseException as error:
            cleanup["error"] = {"type": type(error).__name__, "message": str(error)}
            raise
        finally:
            if session is not None:
                try:
                    session.control("close")
                except Exception as error:
                    cleanup["session_close_error"] = str(error)
            if process is not None:
                if process.poll() is None:
                    try:
                        process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        try:
                            process.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=3)
                cleanup["console_exit_code"] = process.returncode
                cleanup["console_reaped"] = process.poll() is not None
            if master is not None:
                os.close(master)
            if previous_terminal is not None:
                termios.tcsetattr(0, termios.TCSANOW, previous_terminal)
            if session is not None:
                dump(ARTIFACTS / "final-state.json", session.snapshot())
                all_events = []
                cursor = 0
                while batch := session.events(after=cursor, limit=100):
                    all_events.extend(batch)
                    cursor = batch[-1]["sequence"]
                dump(ARTIFACTS / "events.json", all_events)
                shutil.copytree(session.directory, ARTIFACTS / "session-data", dirs_exist_ok=True)
                shutil.copytree(session.workspace, ARTIFACTS / "student-artifacts", dirs_exist_ok=True)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=3)
            cleanup["server_thread_alive"] = server_thread.is_alive()
            cleanup["server_closed"] = server.socket.fileno() == -1
            cleanup["http_request_count"] = len(FixtureHandler.requests)
            cleanup["completed_at"] = now()
            cleanup["elapsed_seconds"] = round(time.monotonic() - started, 3)
            dump(ARTIFACTS / "runtime-hashes-after.json", {name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() for name in runtime_files})
            dump(ARTIFACTS / "document-hashes-after.json", {name: {"sha256": hashlib.sha256((SOURCE / name).read_bytes()).hexdigest(), "bytes": (SOURCE / name).stat().st_size} for name in DOCS})
            dump(ARTIFACTS / "cleanup.json", cleanup)
    cleanup["temporary_root_removed"] = not root.exists()
    dump(ARTIFACTS / "cleanup.json", cleanup)


if __name__ == "__main__":
    main()
