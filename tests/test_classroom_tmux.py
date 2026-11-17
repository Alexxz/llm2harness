"""Изолированный tmux, настоящая PTY и синтетический HTTP вместо модели."""

from contextlib import redirect_stdout
import errno
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from unittest.mock import patch

from coursekit.classroom import Session
from coursekit.classroom_tmux import CHILD_CODE, ExperimentConsole, LessonBoard, TmuxServer, board_text, child_argv, refresh_interface, run_split


def make_session(root, endpoint="http://127.0.0.1:8088"):
    lesson = root / "lessons/01-black-box"
    (lesson / "prompts").mkdir(parents=True)
    (lesson / "lesson.json").write_text(json.dumps({"id": "01", "experiments": [], "initial_generation": {"temperature": 0, "seed": 42, "max_tokens": 96}}))
    (lesson / "INTRO.md").write_text("tmux-intro-unique: Сначала объяснение.\nВход: {{PROMPT}}\nЗатем одно задание.", encoding="utf-8")
    (lesson / "prompts/baseline.txt").write_text("tmux-fixture-input", encoding="utf-8")
    return Session.create("01", root=root, endpoint=endpoint)


class TmuxFixture(BaseHTTPRequestHandler):
    requests = []
    release = threading.Event()

    def log_message(self, *args):
        pass

    def do_POST(self):
        type(self).requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        type(self).release.wait(5)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"content":"synthetic-response","tokens_predicted":2}')
        except (BrokenPipeError, ConnectionResetError):
            pass


class ClassroomTmuxTests(unittest.TestCase):
    def test_lesson_prose_wraps_at_spaces_and_keeps_terminal_controls_literal(self):
        paragraph = "Преподаватель объясняет назначение интерфейса перед выполнением упражнения."
        rendered = board_text("# Знакомство с интерфейсом\n\n" + paragraph, columns=40, markdown=True)
        self.assertIn("Знакомство с интерфейсом", rendered)
        self.assertNotIn("# ", rendered)
        self.assertNotIn("| ", rendered)
        self.assertTrue(all(len(line) <= 38 for line in rendered.splitlines()))
        self.assertEqual(" ".join(rendered.splitlines()[2:]).split(), paragraph.split())
        unsafe = board_text("текст\x1b]52;c;placeholder\x07\u202e", columns=60)
        self.assertNotIn("\x1b", unsafe)
        self.assertNotIn("\u202e", unsafe)
        self.assertIn("\\u001b", unsafe)

    def test_child_arguments_do_not_interpolate_paths_or_use_personal_shell(self):
        class Fixture:
            id = "lesson-01-abcdef123456"
            root = Path("/tmp/course $(must-not-run); 'quote'")
        argv = child_argv(Fixture(), "board")
        self.assertEqual(argv[:4], [sys.executable, "-I", "-c", CHILD_CODE])
        self.assertEqual(argv[-3:], [str(Fixture.root), Fixture.id, "board"])
        self.assertNotIn(str(Fixture.root), CHILD_CODE)
        self.assertNotIn("/bin/sh", argv)
        server = TmuxServer("/usr/bin/tmux", "/tmp/owned/socket")
        self.assertEqual(server.prefix[1:3], ["-f", "/dev/null"])
        self.assertNotIn("TMUX", server.environment)
        self.assertEqual(server.environment["HOME"], str(Path.home()))

    def test_experiment_routes_lesson_help_and_questions_without_model_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = make_session(Path(temporary))
            console = ExperimentConsole(session)
            with patch("coursekit.model.ModelClient.generate", side_effect=AssertionError("No generation")), redirect_stdout(io.StringIO()) as output:
                console.welcome()
                console.execute("lesson")
                console.execute("help")
                console.execute("ask Зачем эта команда?")
                console.execute("show")
            text = output.getvalue()
            self.assertIn("tmux-fixture-input", text)
            self.assertNotIn("tmux-intro-unique", text)
            self.assertNotIn("Сначала ориентирование", text)
            events = session.events(limit=100)
            self.assertTrue(any(e["event"] == "lesson_intro_shown" and "tmux-intro-unique" in e["data"]["text"] for e in events))
            self.assertTrue(any(e["event"] == "lesson_help_shown" for e in events))
            self.assertFalse(any(e["event"] in {"run_started", "model_request"} for e in events))
            self.assertEqual(session.snapshot()["prompt"], "tmux-fixture-input")

    def test_board_delivers_plain_messages_to_independent_teacher(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = make_session(root)
            (root / "course").mkdir()
            (root / "course/TERMINAL_BASICS.md").write_text("interface-basics-unique", encoding="utf-8")
            board = LessonBoard(session, input_stream=io.StringIO("Мой вопрос с 'кавычкой'\n/basics\n/lesson\n/exit\n"))
            with patch("coursekit.teacher.TeacherRuntime"), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(board.run(), 0)
            events = session.events(limit=100)
            questions = [e for e in events if e["event"] == "student_question"]
            self.assertEqual(questions[0]["data"]["message"], "Мой вопрос с 'кавычкой'")
            self.assertIn("interface-basics-unique", output.getvalue())
            self.assertIn("tmux-intro-unique", output.getvalue())
            self.assertIn("Codex CLI", output.getvalue())
            self.assertNotIn("Реплика сохранена", output.getvalue())
            self.assertFalse(any(e["event"] in {"owner_attached", "run_started", "model_request"} for e in events))
            self.assertEqual(session.snapshot()["gate"], "closed")
            self.assertEqual(session.snapshot()["prompt"], "tmux-fixture-input")

    def test_noninteractive_launcher_does_not_start_any_process(self):
        with patch("coursekit.classroom_tmux.sys.stdin", io.StringIO()), patch("coursekit.classroom_tmux.subprocess.Popen") as process:
            with self.assertRaisesRegex(RuntimeError, "интерактивного"):
                run_split(None)
            process.assert_not_called()

    def test_refresh_rejects_a_socket_outside_the_owned_course_server(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = make_session(Path(temporary))
            session._record("layout_started", "system", {"socket": "/tmp/another-tmux/socket", "teacher_pane": "%0", "experiment_pane": "%1"})
            with session.claim_owner(), patch.object(TmuxServer, "command") as command:
                with self.assertRaisesRegex(ValueError, "собственный socket"):
                    refresh_interface(session)
                command.assert_not_called()

    @unittest.skipUnless(shutil.which("tmux"), "tmux executable is required for live PTY proof")
    def test_real_tmux_draft_advice_experiment_and_detach_cleanup(self):
        source = Path(__file__).resolve().parents[1]
        server = ThreadingHTTPServer(("127.0.0.1", 0), TmuxFixture)
        server.daemon_threads = True
        TmuxFixture.requests, TmuxFixture.release = [], threading.Event()
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        self.addCleanup(server_thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(TmuxFixture.release.set)
        with tempfile.TemporaryDirectory(prefix="classroom-tmux-") as temporary:
            root = Path(temporary) / "course space 'quote' $(touch must-not-exist)"
            root.mkdir()
            session = make_session(root, endpoint=f"http://127.0.0.1:{server.server_port}")
            prose = " ".join(["читабельность предложение интерфейс"] * 8)
            (root / "lessons/01-black-box/INTRO.md").write_text("# tmux-intro-unique\n\n" + prose, encoding="utf-8")
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 140, 0, 0))
            code = "from pathlib import Path; from coursekit.classroom import Session; from coursekit.classroom_tmux import run_split; import sys; raise SystemExit(run_split(Session.open(sys.argv[1], root=Path(sys.argv[2]))))"
            fixture = Path(temporary) / "teacher-fixture"
            fixture.write_text('#!/usr/bin/python3\nimport sys,json,time\nfrom pathlib import Path\nsys.stdin.read()\ntime.sleep(.8)\nPath(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({"reply":"synthetic-teacher-response", "note":"fixture", "control":"none"}))\n')
            fixture.chmod(0o700)
            env = {"COURSE_TEACHER_BACKEND": "codex", "COURSE_TEACHER_COMMAND": str(fixture), "PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8", "TERM": "xterm-256color"}
            process = subprocess.Popen([sys.executable, "-c", code, session.id, str(root)], cwd=source, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
            os.close(slave)
            transcript, layout = bytearray(), None

            def pump():
                if select.select([master], [], [], .03)[0]:
                    try:
                        transcript.extend(os.read(master, 65536))
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise

            def until(predicate, label, timeout=8):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    pump()
                    if predicate():
                        return
                    if process.poll() is not None:
                        self.fail(f"tmux ended before {label}: {transcript.decode('utf-8', errors='replace')[-2500:]}")
                self.fail(f"Timeout awaiting {label}: {transcript.decode('utf-8', errors='replace')[-2500:]}")

            def tmux(*arguments):
                result = subprocess.run([shutil.which("tmux"), "-f", "/dev/null", "-S", layout["socket"], *arguments], capture_output=True, text=True, timeout=5, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout

            try:
                until(lambda: any(e["event"] == "layout_started" for e in session.events(limit=100)), "owned layout")
                layout = next(e["data"] for e in session.events(limit=100) if e["event"] == "layout_started")
                until(lambda: session.snapshot()["owner_pid"] is not None and any(e["event"] == "lesson_board_ready" for e in session.events(limit=100)), "both plain Python panes")
                until(lambda: "Преподаватель готовит ответ" in tmux("capture-pane", "-p", "-S", "-", "-t", layout["teacher_pane"]), "visible teacher activity hook")
                os.write(master, b"/lesson\r")
                until(lambda: "tmux-intro-unique" in tmux("capture-pane", "-p", "-t", layout["teacher_pane"]), "lesson rendered in left pane")
                lesson_view = tmux("capture-pane", "-p", "-S", "-", "-t", layout["teacher_pane"])
                self.assertNotIn("| ", lesson_view)
                self.assertNotIn("# tmux-intro", lesson_view)
                self.assertNotIn("Реплика сохранена", lesson_view)
                for word in ("читабельность", "предложение", "интерфейс"):
                    self.assertEqual(lesson_view.split().count(word), 8, lesson_view)
                self.assertIn("\x1b[", tmux("capture-pane", "-e", "-p", "-t", layout["teacher_pane"]))
                owner = session.snapshot()["owner_pid"]
                self.assertTrue(refresh_interface(session, redraw=True)["board_redrawn"])
                until(lambda: sum(e["event"] == "lesson_board_ready" for e in session.events(limit=100)) >= 2, "refreshed teacher terminal")
                self.assertEqual(session.snapshot()["owner_pid"], owner)
                self.assertEqual(session.snapshot()["gate"], "paused")
                self.assertIn("Ctrl+h", tmux("show-options", "-gv", "status-left"))
                self.assertIn("ПРЕПОДАВАТЕЛЯ", tmux("display-message", "-p", "-t", layout["teacher_pane"], "#{pane_title}"))
                self.assertIn("УЧЕНИКА", tmux("display-message", "-p", "-t", layout["experiment_pane"], "#{pane_title}"))
                os.write(master, b"\x1bOS")  # F4 opens the saved result without executing an experiment.
                until(lambda: any(e["event"] == "interface_result_opened" for e in session.events(limit=100)), "F4 result viewer")
                self.assertEqual(TmuxFixture.requests, [])
                os.write(master, b"\x1b")
                until(lambda: any(e["event"] == "interface_result_closed" for e in session.events(limit=100)), "close result viewer")
                result_pid = next(e["data"]["process_id"] for e in session.events(limit=100) if e["event"] == "interface_result_opened")
                until(lambda: not Path(f"/proc/{result_pid}").exists(), "result popup reaped")
                os.write(master, b"\x1b[20~")  # F9 playground
                until(lambda: any(e["event"] == "playground_opened" for e in session.events(limit=100)), "F9 playground opens")
                os.write(master, b"\x15sandbox-edit\t12\t0.7\t23")
                os.write(master, b"\x1b[15~")  # F5 sends the selected settings.
                until(lambda: bool(TmuxFixture.requests), "playground HTTP request")
                self.assertEqual(TmuxFixture.requests[0]["prompt"], "sandbox-edit")
                self.assertEqual(TmuxFixture.requests[0]["n_predict"], 12)
                self.assertEqual(TmuxFixture.requests[0]["temperature"], .7)
                self.assertEqual(TmuxFixture.requests[0]["seed"], 23)
                self.assertEqual(session.snapshot()["prompt"], "tmux-fixture-input")
                self.assertEqual(session.snapshot()["settings"]["max_tokens"], 96)
                os.write(master, b"\x1b[17~")  # F6 stops only the playground worker.
                until(lambda: any(e["event"] == "playground_result" for e in session.events(limit=100)), "playground stop receipt")
                TmuxFixture.release.set()
                os.write(master, b"\x1b[18~\x15second-prompt\x1b[19~24\t0.2\x1b[15~")  # F7 edit, F8 params, F5 repeat.
                until(lambda: len(TmuxFixture.requests) == 2, "second request in the same playground")
                until(lambda: sum(e["event"] == "playground_result" for e in session.events(limit=100)) == 2, "second playground response")
                self.assertEqual(TmuxFixture.requests[1]["prompt"], "second-prompt")
                self.assertEqual(TmuxFixture.requests[1]["n_predict"], 24)
                self.assertEqual(TmuxFixture.requests[1]["temperature"], .2)
                os.write(master, b"\x1b[18~\x15third-prompt\x1b[19~16\x1b[15~")
                until(lambda: len(TmuxFixture.requests) == 3, "third request after a completed response")
                until(lambda: sum(e["event"] == "playground_result" for e in session.events(limit=100)) == 3, "third playground response")
                self.assertEqual(TmuxFixture.requests[2]["prompt"], "third-prompt")
                self.assertEqual(TmuxFixture.requests[2]["n_predict"], 16)
                self.assertEqual(session.snapshot()["prompt"], "tmux-fixture-input")
                os.write(master, b"\x1b")
                until(lambda: any(e["event"] == "playground_closed" for e in session.events(limit=100)), "playground closes")
                # The child records closure before tmux removes its popup.
                # Synchronize the parent popup before sending another root key.
                tmux("display-popup", "-C", "-t", "course")
                self.assertEqual(session.snapshot()["gate"], "paused")
                TmuxFixture.requests = []
                TmuxFixture.release.clear()
                os.write(master, b"\x1bOR")  # F3, without a prefix
                until(lambda: tmux("display-message", "-p", "-t", "course", "#{pane_id}").strip() == layout["experiment_pane"], "switch to student terminal")
                os.write(master, b"\x08")
                until(lambda: any(e["event"] == "interface_help_opened" for e in session.events(limit=100)), "Ctrl+h help from student terminal")
                os.write(master, b"\x1b")
                until(lambda: any(e["event"] == "interface_help_closed" for e in session.events(limit=100)), "return from help")
                help_pid = next(e["data"]["process_id"] for e in session.events(limit=100) if e["event"] == "interface_help_opened")
                until(lambda: not Path(f"/proc/{help_pid}").exists(), "popup process reaped before next hotkey")
                self.assertEqual(session.snapshot()["prompt"], "tmux-fixture-input")
                os.write(master, b"\x1bOQ")  # F2, without a prefix
                until(lambda: tmux("display-message", "-p", "-t", "course", "#{pane_id}").strip() == layout["teacher_pane"], "switch back to teacher terminal")
                self.assertEqual(TmuxFixture.requests, [])
                os.write(master, b"draft-before-")
                until(lambda: "draft-before-" in tmux("capture-pane", "-p", "-t", layout["teacher_pane"]), "partial question")
                os.write(master, b"\x08")
                until(lambda: sum(e["event"] == "interface_help_opened" for e in session.events(limit=100)) == 2, "help during unfinished message")
                os.write(master, b"\x1b")
                until(lambda: sum(e["event"] == "interface_help_closed" for e in session.events(limit=100)) == 2, "return to unfinished message")
                help_pid = [e["data"]["process_id"] for e in session.events(limit=100) if e["event"] == "interface_help_opened"][-1]
                until(lambda: not Path(f"/proc/{help_pid}").exists(), "second popup process reaped")
                session.advise("hint-during-draft-unique")
                until(lambda: "hint-during-draft-unique" in tmux("capture-pane", "-p", "-t", layout["teacher_pane"]), "live advice")
                os.write(master, b"after-advice\r")
                until(lambda: any(e["event"] == "student_question" for e in session.events(limit=100)), "delivery to the teacher")
                question = next(e for e in session.events(limit=100) if e["event"] == "student_question")
                self.assertEqual(question["data"]["message"], "draft-before-after-advice")
                until(lambda: "synthetic-teacher-response" in tmux("capture-pane", "-p", "-S", "-", "-t", layout["teacher_pane"]), "independent teacher reply")
                self.assertEqual(TmuxFixture.requests, [])
                self.assertEqual(session.snapshot()["prompt"], "tmux-fixture-input")
                session.control("resume")
                tmux("select-pane", "-t", layout["experiment_pane"])
                os.write(master, b"run\r")
                until(lambda: bool(TmuxFixture.requests), "synthetic HTTP experiment")
                self.assertEqual(TmuxFixture.requests[0]["n_predict"], 96)
                self.assertEqual(TmuxFixture.requests[0]["temperature"], 0)
                self.assertEqual(TmuxFixture.requests[0]["seed"], 42)
                self.assertIsNotNone(session.snapshot()["active_run"])
                session.advise("hint-while-running-unique")
                until(lambda: "hint-while-running-unique" in tmux("capture-pane", "-p", "-t", layout["teacher_pane"]), "advice during experiment")
                self.assertNotIn("hint-while-running-unique", tmux("capture-pane", "-p", "-t", layout["experiment_pane"]))
                os.write(master, b"\x02d")  # ordinary tmux detach; launcher must close safely
                deadline = time.monotonic() + 15
                while process.poll() is None and time.monotonic() < deadline:
                    pump()
                process.wait(timeout=1)
                self.assertEqual(process.returncode, 0, transcript.decode("utf-8", errors="replace")[-3000:])
                state = session.snapshot()
                self.assertEqual(state["gate"], "closed")
                self.assertIsNone(state["active_run"])
                self.assertIsNone(state["owner_pid"])
                self.assertEqual(state["last_run"]["status"], "cancelled")
                self.assertEqual(state["last_run"]["cleanup"], "confirmed")
                self.assertEqual(len(TmuxFixture.requests), 1)
                self.assertFalse((root / "must-not-exist").exists())
                closed = next(e["data"] for e in session.events(limit=100) if e["event"] == "layout_closed")
                self.assertTrue(closed["owned_tmux_stopped"])
                self.assertTrue(closed["owner_released"])
                self.assertFalse(Path(layout["socket"]).parent.exists())
            finally:
                TmuxFixture.release.set()
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=18)
                    except subprocess.TimeoutExpired:
                        if layout:
                            subprocess.run([shutil.which("tmux"), "-f", "/dev/null", "-S", layout["socket"], "kill-server"], capture_output=True, timeout=5, env=env)
                        process.kill()
                        process.wait(timeout=3)
                os.close(master)


if __name__ == "__main__":
    unittest.main()
