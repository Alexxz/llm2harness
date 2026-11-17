"""Проверки границы терминала и явного выбора действий учеником."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coursekit.classroom import Session
from coursekit.classroom_console import StudentConsole, _show_teacher_event, main, terminal_text


class ConsoleSession:
    def __init__(self):
        self.prompt = "original"
        self.calls = []
        self.submitted = []

    def snapshot(self):
        return {"gate": "open", "prompt": self.prompt, "settings": {}}

    def events(self, **kwargs):
        return []

    def set_prompt(self, value):
        self.prompt = value

    def run(self, kind, **kwargs):
        self.calls.append((kind, kwargs))
        return {"status": "completed"}

    def ask(self, message):
        self.calls.append(("question", message))

    def record_command(self, command):
        self.submitted.append(command)


class ClassroomConsoleTests(unittest.TestCase):
    def test_opening_explains_the_actual_input_without_calling_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lesson = root / "lessons/00-interface"
            (lesson / "prompts").mkdir(parents=True)
            (lesson / "lesson.json").write_text(json.dumps({"id": "00", "experiments": []}))
            (lesson / "prompts/baseline.txt").write_text("Точный вход для опыта\n", encoding="utf-8")
            (lesson / "INTRO.md").write_text("Сначала объяснение.\n{{PROMPT}}\nЗатем одно задание.", encoding="utf-8")
            session = Session.create("00", root=root)
            self.assertEqual(session.snapshot()["capabilities"], ["doctor"])
            console = StudentConsole(session, input_stream=io.StringIO("lesson\nshow\nask Пока разбираюсь с входом\nexit\n"))
            with patch("coursekit.model.ModelClient.generate", side_effect=AssertionError("Ориентация не вызывает модель")), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(console.run(), 0)
            rendered = output.getvalue()
            self.assertIn("Точный вход для опыта", rendered)
            self.assertNotIn("{{PROMPT}}", rendered)
            self.assertIn("ТЕКСТ ДЛЯ МОДЕЛИ — ВХОД", rendered)
            self.assertIn("Команда show только показывает", rendered)
            self.assertIn("канале преподавателя", rendered)
            events = session.events(limit=100)
            self.assertEqual(sum(e["event"] == "lesson_intro_shown" for e in events), 2)
            self.assertFalse(any(e["event"] in {"run_started", "model_request"} for e in events))
            self.assertEqual(session.snapshot()["prompt"], "Точный вход для опыта\n")
            self.assertIsNone(session.snapshot()["owner_pid"])

    def test_doctor_summary_does_not_claim_a_running_model(self):
        session = ConsoleSession()
        session.run = lambda *args, **kwargs: {"status": "completed", "result": {"python": "3.12", "llama_server": "/course/runtime", "local_weights": [], "network_requests": 0}}
        with redirect_stdout(io.StringIO()) as output:
            StudentConsole(session).execute("doctor")
        rendered = output.getvalue()
        self.assertIn("Файлов весов в каталоге курса: 0", rendered)
        self.assertIn("не подтверждает готовность модели", rendered)
        self.assertIn("Сетевых запросов этой проверки: 0", rendered)

    def test_terminal_data_cannot_issue_ansi_osc_or_bidi_commands(self):
        rendered = terminal_text("данные\x1b]52;c;placeholder\x07\x1b[2J\u202e")
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x07", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertIn("данные", rendered)
        self.assertIn("\\u001b", rendered)

    def test_displayed_advice_does_not_execute_or_change_prompt(self):
        session = ConsoleSession()
        console = StudentConsole(session)
        with redirect_stdout(io.StringIO()) as output:
            _show_teacher_event({"role": "teacher", "event": "advice", "data": {"message": "run; ignore previous prompt"}})
        self.assertIn("ПРЕПОДАВАТЕЛЬ", output.getvalue())
        self.assertEqual(session.prompt, "original")
        self.assertEqual(session.calls, [])
        self.assertIsNotNone(console)

    def test_shell_syntax_and_generic_argument_passthrough_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "must-not-exist"
            session = ConsoleSession()
            console = StudentConsole(session)
            for command in (f"touch {target}", f"run; touch {target}", f"run --endpoint http://127.0.0.1:1", f"/bin/bash -c 'touch {target}'"):
                with self.assertRaises(ValueError):
                    console.execute(command)
            self.assertFalse(target.exists())
            self.assertEqual(session.calls, [])

    def test_multiline_prompt_is_data_until_explicit_run(self):
        session = ConsoleSession()
        console = StudentConsole(session, input_stream=io.StringIO("$(do-not-execute)\n::end\n"))
        with redirect_stdout(io.StringIO()):
            console.execute("edit")
        self.assertEqual(session.prompt, "$(do-not-execute)\n")
        self.assertEqual(session.calls, [])
        with redirect_stdout(io.StringIO()):
            console.execute("run")
        self.assertEqual(session.calls[0][0], "complete")

    def test_terminal_question_redirects_to_chat_without_fake_delivery(self):
        session = ConsoleSession()
        console = StudentConsole(session)
        with redirect_stdout(io.StringIO()) as output:
            console.execute("ask Почему ответ изменился?")
        self.assertEqual(session.calls, [])
        self.assertIn("канале преподавателя", output.getvalue())

    def test_cancelled_edit_preserves_original_text(self):
        session = ConsoleSession()
        console = StudentConsole(session, input_stream=io.StringIO("draft\n::cancel\n"))
        with redirect_stdout(io.StringIO()):
            console.execute("edit")
        self.assertEqual(session.prompt, "original")

    def test_advice_between_enter_and_worker_event_is_not_skipped(self):
        session = ConsoleSession()
        session.events = lambda **kwargs: [{"sequence": 1, "role": "teacher", "event": "advice", "data": {"message": "Сохрани прогноз"}}] if kwargs.get("after", 0) < 1 else []
        console = StudentConsole(session)
        with redirect_stdout(io.StringIO()) as output:
            console.run_event({"sequence": 2, "role": "worker", "event": "worker_phase", "data": {"phase": "starting"}})
        self.assertIn("Сохрани прогноз", output.getvalue())
        self.assertEqual(console.cursor, 2)

    def test_json_observation_is_lossless_but_cannot_control_a_terminal(self):
        session = ConsoleSession()
        session.prompt = "вход\x9b2J\u202e"
        with patch("coursekit.classroom.Session.open", return_value=session), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["observe", "safe-session", "--json"]), 0)
        self.assertNotIn("\x9b", output.getvalue())
        self.assertNotIn("\u202e", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["session"]["prompt"], session.prompt)

    def test_pasted_multiline_input_does_not_wait_for_an_extra_keystroke(self):
        read_descriptor, write_descriptor = os.pipe()
        try:
            os.write(write_descriptor, "первая\nвторая\n::end\n".encode("utf-8"))
            with os.fdopen(read_descriptor, "r", encoding="utf-8") as stream:
                read_descriptor = None
                session = ConsoleSession()
                console = StudentConsole(session, input_stream=stream)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(console.line(), "первая\n")
                    self.assertEqual(console.line(), "вторая\n")
                    self.assertEqual(console.line(), "::end\n")
        finally:
            if read_descriptor is not None:
                os.close(read_descriptor)
            os.close(write_descriptor)


if __name__ == "__main__":
    unittest.main()
