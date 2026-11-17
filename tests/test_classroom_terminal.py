"""Две настоящие роли и PTY; вместо LLM — явно синтетический HTTP сервер."""

import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from coursekit.classroom import Session


class FixtureHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(request)
        if "slow-experiment" in request["prompt"]:
            time.sleep(4)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"content": "fixture-completion-unique", "tokens_predicted": 2}).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass


class ClassroomTerminalTests(unittest.TestCase):
    def test_student_experiments_while_teacher_observes_advises_and_stops(self):
        source = Path(__file__).resolve().parents[1]
        server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        server.daemon_threads = True
        FixtureHandler.requests = []
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        self.addCleanup(server_thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix="classroom-terminal-") as temporary:
            root = Path(temporary)
            lesson = root / "lessons/01-black-box"
            (lesson / "prompts").mkdir(parents=True)
            (lesson / "lesson.json").write_text(json.dumps({"id": "01", "experiments": []}))
            (lesson / "prompts/baseline.txt").write_text("initial fixture prompt", encoding="utf-8")
            (lesson / "prompts/variation.txt").write_text("variation fixture prompt", encoding="utf-8")
            (lesson / "INTRO.md").write_text("terminal-intro-unique: Это учебная программа.\nВход: {{PROMPT}}\nСначала разбираем задачу.", encoding="utf-8")
            session = Session.create("01", root=root, endpoint=f"http://127.0.0.1:{server.server_port}")
            master, slave = pty.openpty()
            code = "from pathlib import Path; from coursekit.classroom_console import main; import sys; raise SystemExit(main(['student', sys.argv[1]], root=Path(sys.argv[2])))"
            process = subprocess.Popen([sys.executable, "-c", code, session.id, str(root)], cwd=source, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave)
            transcript = bytearray()

            def pump():
                if select.select([master], [], [], 0.03)[0]:
                    try:
                        transcript.extend(os.read(master, 65536))
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise

            def until(predicate, label, timeout=6):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    pump()
                    if predicate():
                        return
                    if process.poll() is not None:
                        self.fail(f"Консоль завершилась до {label}: {transcript.decode('utf-8', errors='replace')[-3000:]}")
                self.fail(f"Не дождались {label}: {transcript.decode('utf-8', errors='replace')[-3000:]}")

            def send(text):
                os.write(master, text.encode("utf-8"))

            try:
                until(lambda: transcript.endswith("ученик> ".encode("utf-8")), "приглашения после введения")
                self.assertIn(b"terminal-intro-unique", transcript)
                self.assertIn(b"initial fixture prompt", transcript)
                self.assertNotIn(b"{{PROMPT}}", transcript)
                self.assertEqual(FixtureHandler.requests, [])
                session.advise("advisor-message-unique: сначала один фактор")
                until(lambda: b"advisor-message-unique" in transcript, "совета при ожидании ввода")
                send("doctor\n")
                until(lambda: session.snapshot().get("last_run") is not None and session.snapshot().get("active_run") is None, "doctor без сети")
                self.assertEqual(FixtureHandler.requests, [])

                send("edit\nмой прогноз\n::end\n")
                until(lambda: session.snapshot()["prompt"] == "мой прогноз\n", "многострочной вставки")
                send("run\n")
                until(lambda: "ОПЫТ НЕ ВЫПОЛНЕН" in transcript.decode("utf-8", errors="replace"), "отказа на паузе")
                self.assertEqual(FixtureHandler.requests, [])

                session.control("resume")
                send("run\n")
                until(lambda: b"fixture-completion-unique" in transcript and session.snapshot()["active_run"] is None, "настоящего HTTP ответа фикстуры")
                self.assertEqual(len(FixtureHandler.requests), 1)
                self.assertEqual(FixtureHandler.requests[0]["prompt"], "мой прогноз\n")

                send("ask Почему результат такой?\n")
                until(lambda: any(event["event"] == "student_command" and event["data"]["command"] == "ask" for event in session.events(limit=100)), "перенаправления вопроса в обычный чат")
                self.assertFalse(any(event["event"] == "student_question" for event in session.events(limit=100)))
                send("edit\nslow-experiment\n::end\nrun\n")
                until(lambda: len(FixtureHandler.requests) == 2, "запущенного медленного опыта")
                active = session.snapshot()["active_run"]
                self.assertIsNotNone(active)
                session.advise("during-run-message-unique")
                until(lambda: b"during-run-message-unique" in transcript, "совета во время вычисления")
                session.control("stop", target_run_id=active["id"])
                until(lambda: session.snapshot()["active_run"] is None, "подтверждённой остановки", timeout=10)
                stopped = session.snapshot()
                self.assertEqual(stopped["gate"], "paused")
                self.assertEqual(stopped["last_run"]["status"], "cancelled")
                self.assertEqual(stopped["last_run"]["cleanup"], "confirmed")
                self.assertTrue(any(event["event"] == "control_applied" for event in session.events(limit=100)))
                session.control("close")
                process.wait(timeout=4)
                self.assertEqual(process.returncode, 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                os.close(master)


if __name__ == "__main__":
    unittest.main()
