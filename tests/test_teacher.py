import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from coursekit.classroom import Session
from coursekit.teacher import CodexTeacher, TeacherRuntime

class Backend:
    def __init__(self):
        self.calls = []
    def answer(self, events):
        self.calls.append(events)
        return {'reply': 'Открой помощь: Ctrl+h.', 'note': 'Помощь ещё не проверена.'}
    def close(self):
        pass

class TeacherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        lesson = self.root / 'lessons/00-interface'
        (lesson / 'prompts').mkdir(parents=True)
        (lesson / 'lesson.json').write_text(json.dumps({'id': '00', 'experiments': []}))
        (lesson / 'prompts/baseline.txt').write_text('test-input')
        self.session = Session.create('00', root=self.root)
    def wait_reply(self, count):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            replies = [e for e in self.session.events(limit=100) if e['event'] == 'teacher_turn_done']
            if len(replies) >= count:
                return replies
            time.sleep(.02)
        self.fail('Teacher did not reply independently')
    def test_dialogue_observation_and_restart(self):
        backend = Backend()
        runtime = TeacherRuntime(self.session, backend)
        self.session.ask('Где писать вопрос?')
        runtime.start()
        try:
            self.wait_reply(1)
            self.session.record_command('show')
            self.session._record('prompt_shown', 'student', {'prompt':'test-input', 'model_called':False})
            time.sleep(.4)
            self.assertEqual(len(backend.calls), 1)
            self.session.ask('Вход вижу, но есть вопрос')
            self.wait_reply(2)
            self.assertTrue(any(e['event'] == 'prompt_shown' for e in backend.calls[-1]))
        finally:
            runtime.close()
        runtime = TeacherRuntime(self.session, backend)
        runtime.start()
        try:
            time.sleep(.5)
            self.assertEqual(len(backend.calls), 2)
            self.session.ask('Это ответ модели?')
            self.wait_reply(3)
        finally:
            runtime.close()
    def test_failure_requires_explicit_retry(self):
        backend = Backend()
        backend.answer = lambda _: (_ for _ in ()).throw(RuntimeError('test-unavailable'))
        runtime = TeacherRuntime(self.session, backend)
        self.session.ask('Вопрос')
        runtime.start()
        try:
            time.sleep(.6)
            failures = [e for e in self.session.events(limit=100) if e['event'] == 'teacher_turn_failed']
            self.assertEqual(len(failures), 1)
        finally:
            runtime.close()
    def test_real_subprocess_adapter_with_synthetic_executable(self):
        command = self.root / 'fixture'
        command.write_text('#!/usr/bin/python3\nimport sys,json\nfrom pathlib import Path\nprompt=sys.stdin.read()\nassert "test-input" in prompt\nPath(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({"reply":"synthetic teacher", "note":"fixture", "control":"none"}))\n')
        command.chmod(0o700)
        teacher = CodexTeacher(self.session, command=str(command))
        result = teacher.answer([])
        self.assertEqual(result['reply'], 'synthetic teacher')

    def test_timeout_kills_owned_process(self):
        command = self.root / 'slow-fixture'
        command.write_text('#!/usr/bin/python3\nimport sys,time\nsys.stdin.read()\ntime.sleep(30)\n')
        command.chmod(0o700)
        teacher = CodexTeacher(self.session, command=str(command), timeout=.1)
        with self.assertRaisesRegex(RuntimeError, 'не ответил'):
            teacher.answer([])
        self.assertIsNotNone(teacher.process.poll())

    def test_explicit_retry_delivers_after_failure(self):
        backend = Backend()
        original = backend.answer
        backend.answer = lambda _: (_ for _ in ()).throw(RuntimeError('unavailable'))
        runtime = TeacherRuntime(self.session, backend)
        self.session.ask('Вопрос')
        runtime.start()
        try:
            deadline = time.monotonic() + 3
            while not any(e['event'] == 'teacher_turn_failed' for e in self.session.events(limit=100)):
                if time.monotonic() > deadline:
                    self.fail('Missing explicit error')
                time.sleep(.02)
            backend.answer = original
            self.session._record('teacher_retry', 'student', {})
            self.wait_reply(1)
        finally:
            runtime.close()

    def test_prepared_first_task_is_immediate_without_model_call(self):
        lesson = self.root / 'lessons/00-interface'
        (lesson / 'TEACHER_START.md').write_text('Первое задание: нажми F3.', encoding='utf-8')
        backend = Backend()
        runtime = TeacherRuntime(self.session, backend)
        self.session._record('lesson_board_ready', 'system', {})
        runtime.start()
        try:
            reply = self.wait_reply(1)[0]
            self.assertEqual(reply['data']['source'], 'prepared')
            self.assertEqual(backend.calls, [])
            self.session.ask('Не получается переключиться')
            self.wait_reply(2)
            self.assertEqual(len(backend.calls), 1)
        finally:
            runtime.close()

    def test_settings_and_prompt_advance_without_another_student_message(self):
        backend = Backend()
        runtime = TeacherRuntime(self.session, backend)
        self.session.ask('Мой прогноз')
        runtime.start()
        try:
            self.wait_reply(1)
            self.session.set_setting('temperature', 0)
            self.wait_reply(2)
            self.assertEqual(backend.calls[-1][-1]['event'], 'setting_changed')
            self.assertEqual(backend.calls[-1][-1]['data']['value'], 0)
            self.session.set_setting('seed', 42)
            self.wait_reply(3)
            self.session.set_setting('max_tokens', 96)
            self.wait_reply(4)
            self.session.set_prompt('Изменённый вход')
            time.sleep(.4)
            self.assertEqual(len(backend.calls), 4)
            self.session.ask('Почему новый вход изменил результат?')
            self.wait_reply(5)
            self.assertTrue(any(e['event']=='prompt_changed' for e in backend.calls[-1]))
        finally:
            runtime.close()

    def test_backend_failure_explains_limit_without_exposing_diagnostics(self):
        from coursekit.teacher import backend_failure
        detail = 'rate limit exceeded; private diagnostic marker must stay private'
        message = backend_failure(detail, 7)
        self.assertIn('лимит', message)
        self.assertIn('7', message)
        self.assertNotIn('private diagnostic marker', message)

    def test_first_show_uses_prepared_guidance_without_model_or_repeated_turn(self):
        lesson = self.root / 'lessons/02-dialog'
        (lesson / 'prompts').mkdir(parents=True)
        (lesson / 'lesson.json').write_text(json.dumps({'id':'02','experiments':[]}))
        (lesson / 'prompts/baseline.txt').write_text('example')
        (lesson / 'AFTER_SHOW.md').write_text('Теперь run отправит этот текст модели.')
        (lesson / 'TEACHER_START.md').write_text('Первое действие: show.')
        session = Session.create('02', root=self.root)
        backend = Backend()
        runtime = TeacherRuntime(session, backend)
        session._record('lesson_board_ready', 'system', {})
        session._record('prompt_shown', 'student', {'prompt':'example','model_called':False})
        runtime.start()
        try:
            deadline = time.monotonic() + 3
            while not any(e['event']=='teacher_turn_done' and e['data'].get('source')=='prepared_after_show' for e in session.events(limit=100)):
                if time.monotonic()>deadline:self.fail('Missing deterministic guidance')
                time.sleep(.02)
            self.assertEqual(session.snapshot()['gate'], 'open')
            self.assertEqual(backend.calls, [])
            session._record('prompt_shown', 'student', {'prompt':'example','model_called':False})
            time.sleep(.4)
            self.assertEqual(backend.calls, [])
            self.assertEqual(sum(e['event']=='teacher_turn_done' for e in session.events(limit=100)), 2)
            session.ask('Почему модель продолжает список?')
            deadline = time.monotonic()+3
            while not backend.calls:
                if time.monotonic()>deadline:self.fail('Question did not reach teacher')
                time.sleep(.02)
        finally:runtime.close()
