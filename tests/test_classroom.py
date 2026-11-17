"""No LLM/Docker: filesystem controls and real fixed trusted fixture workers."""

import json
from pathlib import Path
import tempfile
import textwrap
import threading
import unittest
from unittest.mock import patch
import uuid

from coursekit.classroom import ClassroomError, ClassroomLimit, Session
from coursekit.policy import PolicyError


SOURCE_ROOT = Path(__file__).resolve().parents[1]


class ClassroomTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="course-classroom-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        lesson = self.root / "lessons" / "01-test"
        (lesson / "prompts").mkdir(parents=True)
        (lesson / "fixtures").mkdir()
        (lesson / "lesson.json").write_text(json.dumps({"id": "01", "experiments": [{"id": "other-id", "prompt_file": "prompts/baseline.txt"}]}))
        (lesson / "prompts" / "baseline.txt").write_text("baseline prompt")
        (lesson / "prompts" / "variation.txt").write_text("variation prompt")
        (lesson / "fixtures" / "note.txt").write_text("synthetic note")
        self.session = Session.create("01", root=self.root)
        self.fixture_worker = self.root / "fixture_worker.py"
        self.fixture_worker.write_text(textwrap.dedent(f'''\
            import argparse, json, signal, sys, time
            from pathlib import Path
            sys.path.insert(0, {str(SOURCE_ROOT)!r})
            from coursekit.classroom import Session, _atomic
            class Cancelled(BaseException): pass
            def cancel(*args): raise Cancelled()
            parser = argparse.ArgumentParser()
            parser.add_argument('--request')
            parser.add_argument('--result')
            args = parser.parse_args()
            request = json.loads(Path(args.request).read_text())
            session = Session.open(request['session_id'], root=request['root'])
            seed = request['settings']['seed']
            signal.signal(signal.SIGTERM, (lambda *args: None) if seed == 104 else cancel)
            result = {{'run_id': request['run_id'], 'kind': request['kind'], 'status': 'completed', 'cleanup': 'confirmed'}}
            try:
                session._record('worker_phase', 'worker', {{'run_id': request['run_id'], 'phase': 'started'}})
                if seed in {{101, 104}}:
                    while True: time.sleep(0.05)
                elif seed == 103:
                    for index in range(100):
                        time.sleep(0.07)
                        session._record('worker_phase', 'worker', {{'run_id': request['run_id'], 'phase': 'tick', 'index': index}})
                else:
                    time.sleep(0.1)
                if request['kind'] == 'agent':
                    _atomic(Path(request['agent_state_path']), {{'fixture_only': True}}, session.directory)
                    result['status'] = 'waiting_for_user'
                elif request['kind'] == 'continue':
                    result['status'] = 'finished'
                if seed == 105:
                    result['status'] = 'outcome_unknown'
                result['result'] = {{'prompt': request['prompt'], 'settings': request['settings'], 'capabilities': request['capabilities']}}
            except Cancelled:
                session._record('worker_phase', 'worker', {{'run_id': request['run_id'], 'phase': 'cleanup'}})
                result['status'] = 'cancelled'
            _atomic(Path(args.result), result, session.directory)
            '''))

    def test_default_paused_gate_never_spawns_model_worker(self):
        self.assertEqual(self.session.snapshot()["gate"], "paused")
        with self.session.claim_owner(), patch("coursekit.classroom.subprocess.Popen") as popen:
            with self.assertRaises(ClassroomError):
                self.session.run("complete")
            popen.assert_not_called()

    def test_only_one_owner_and_only_owner_may_run(self):
        other = Session.open(self.session.id, root=self.root)
        with self.assertRaises(ClassroomError):
            self.session.run("doctor")
        with self.session.claim_owner():
            with self.assertRaises(ClassroomError):
                with other.claim_owner():
                    pass
            with self.assertRaises(ClassroomError):
                other.run("doctor")
        self.assertIsNone(self.session.snapshot()["owner_pid"])

    def test_real_offline_doctor_uses_fixed_worker(self):
        with self.session.claim_owner():
            result = self.session.run("doctor")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["network_requests"], 0)
        self.assertEqual(result["cleanup"], "confirmed")
        self.assertTrue(any(item["event"] == "worker_phase" for item in self.session.events(limit=100)))

    def test_advice_and_questions_never_change_prompt(self):
        original = self.session.snapshot()["prompt"]
        self.session.advise("Compare only one variable")
        self.session.ask("Why did the response change?")
        self.assertEqual(self.session.snapshot()["prompt"], original)
        events = self.session.events(limit=100)
        self.assertTrue(any(item["event"] == "teacher_advice" and item["role"] == "teacher" for item in events))
        self.assertTrue(any(item["event"] == "student_question" and item["role"] == "student" for item in events))

    def test_permissions_and_setting_validation(self):
        with self.assertRaises(ClassroomError):
            self.session.set_setting("tools", ["run_bash"])
        with self.assertRaises(ClassroomError):
            self.session.set_setting("skill", True)
        self.session.control("permit", capabilities=["agent", "bash", "skill"])
        self.session.set_setting("tools", ["read_file", "run_bash"])
        self.session.set_setting("skill", True)
        self.assertIn("doctor", self.session.snapshot()["capabilities"])
        for name, value in (("max_tokens", 4097), ("temperature", float("nan")), ("seed", True), ("unknown", 1)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.session.set_setting(name, value)

    def test_canonical_prompt_names_and_safe_asset_copy(self):
        self.session.load_prompt("variation")
        self.assertEqual(self.session.snapshot()["prompt"], "variation prompt")
        self.session.load_prompt("other-id")
        self.assertEqual(self.session.snapshot()["prompt"], "baseline prompt")
        self.session.copy_asset("fixtures/note.txt", "notes.txt")
        self.assertEqual(self.session.read_artifact("notes.txt"), "synthetic note")
        for invalid in ("../session.json", ".hidden", "nested/../../escape"):
            with self.subTest(path=invalid), self.assertRaises((ValueError, PolicyError)):
                self.session.write_artifact(invalid, "no")
        outside = self.root / "outside.txt"
        outside.write_text("original")
        (self.session.workspace / "link.txt").symlink_to(outside)
        with self.assertRaises(PolicyError):
            self.session.read_artifact("link.txt")
        self.assertEqual(outside.read_text(), "original")

    def test_schema_external_references_are_rejected(self):
        directory = self.root / "lessons" / "01-test"
        (directory / "schema.json").write_text(json.dumps({"type": "object"}))
        self.session.set_schema("schema.json")
        (directory / "bad-schema.json").write_text(json.dumps({"$ref": "https://example.invalid/schema"}))
        with self.assertRaises(ValueError):
            self.session.set_schema("bad-schema.json")
        self.session.set_schema(None)
        self.assertIsNone(self.session.snapshot()["schema"])

    def test_freeze_edits_only_affect_next_run_and_stream_is_live(self):
        self.session.control("resume")
        teacher = Session.open(self.session.id, root=self.root)
        seen = []

        def observe(item):
            seen.append(item)
            if item["event"] == "worker_phase" and item["data"]["phase"] == "started":
                teacher.set_prompt("next prompt", source="teacher")
                teacher.set_setting("temperature", 0.7)
                teacher.advise("This advice is visible only")
                with self.assertRaises(ClassroomError):
                    teacher.write_artifact("during-run.txt", "no")

        with patch("coursekit.classroom.WORKER_FILE", self.fixture_worker), self.session.claim_owner():
            result = self.session.run("complete", on_event=observe)
        self.assertEqual(result["result"]["prompt"], "baseline prompt")
        self.assertEqual(result["result"]["settings"]["temperature"], 0)
        self.assertEqual(self.session.snapshot()["prompt"], "next prompt")
        self.assertEqual(self.session.snapshot()["settings"]["temperature"], 0.7)
        self.assertTrue(any(item["event"] == "teacher_advice" for item in seen))

    def test_real_subprocess_stop_unwinds_and_acknowledges(self):
        self.session.set_setting("seed", 101)
        self.session.control("resume")
        teacher = Session.open(self.session.id, root=self.root)

        def observe(item):
            if item["event"] == "worker_phase" and item["data"]["phase"] == "started":
                receipt = teacher.control("stop", target_run_id=item["data"]["run_id"])
                self.assertEqual(receipt["status"], "requested")

        with patch("coursekit.classroom.WORKER_FILE", self.fixture_worker), self.session.claim_owner():
            result = self.session.run("complete", on_event=observe)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["cleanup"], "confirmed")
        self.assertEqual(self.session.snapshot()["gate"], "paused")
        applied = [item for item in self.session.events(limit=100) if item["event"] == "control_applied"]
        self.assertTrue(any(item["data"].get("outcome") == "stopped_local_worker" for item in applied))

    def test_delayed_stop_for_old_uuid_does_not_cancel_new_worker(self):
        self.session.control("resume")
        teacher = Session.open(self.session.id, root=self.root)
        with patch("coursekit.classroom.WORKER_FILE", self.fixture_worker), self.session.claim_owner():
            old = self.session.run("complete")["run_id"]
            self.session.set_setting("seed", 103)
            tick_seen = []

            def observe(item):
                if item["event"] == "worker_phase" and item["data"]["phase"] == "started":
                    receipt = teacher.control("stop", target_run_id=old)
                    self.assertEqual(receipt["outcome"], "stale_target")
                if item["event"] == "worker_phase" and item["data"]["phase"] == "tick":
                    tick_seen.append(item)
                    teacher.control("stop", target_run_id=item["data"]["run_id"])

            result = self.session.run("complete", on_event=observe)
        self.assertTrue(tick_seen)
        self.assertNotEqual(old, result["run_id"])
        self.assertEqual(result["status"], "cancelled")

    def test_forced_kill_records_uncertainty_and_blocks_automatic_recovery(self):
        self.session.set_setting("seed", 104)
        self.session.control("resume")

        def observe(item):
            if item["event"] == "worker_phase" and item["data"]["phase"] == "started":
                self.session.control("stop", target_run_id=item["data"]["run_id"])

        with patch("coursekit.classroom.WORKER_FILE", self.fixture_worker), patch("coursekit.classroom.CANCEL_GRACE_SECONDS", 0.15), self.session.claim_owner():
            result = self.session.run("complete", on_event=observe)
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["cleanup"], "cleanup_unconfirmed")
        self.assertTrue(self.session.snapshot()["recovery_required"])
        with self.assertRaises(ClassroomError):
            self.session.control("resume")

    def test_stale_active_owner_never_kills_pid_from_saved_file(self):
        with self.session._lock("metadata.lock"):
            state = self.session._state()
            state["active_run"] = {"id": uuid.uuid4().hex, "kind": "agent", "worker_pid": 99999999}
            state["owner_pid"] = 99999998
            self.session._save(state)
        with patch("coursekit.classroom.os.killpg") as kill, self.session.claim_owner():
            snapshot = self.session.snapshot()
            self.assertEqual(snapshot["last_run"]["status"], "outcome_unknown")
            self.assertTrue(snapshot["recovery_required"])
            kill.assert_not_called()

    def test_continuation_uses_original_settings_after_edits(self):
        self.session.control("permit", capabilities=["agent", "complete"])
        self.session.control("resume")
        with patch("coursekit.classroom.WORKER_FILE", self.fixture_worker), self.session.claim_owner():
            first = self.session.run("agent")
            self.assertEqual(first["status"], "waiting_for_user")
            self.session.set_setting("max_tokens", 512)
            second = self.session.run("continue", input_text="My answer")
        self.assertEqual(second["status"], "finished")
        self.assertEqual(second["result"]["settings"]["max_tokens"], 256)
        self.assertEqual(second["result"]["prompt"], "My answer")
        self.assertEqual(self.session.snapshot()["settings"]["max_tokens"], 512)

    def test_journal_and_user_text_are_bounded(self):
        with self.assertRaises(ValueError):
            self.session.set_prompt("x" * 65537)
        with patch("coursekit.classroom.MAX_EVENT_BYTES", 300), self.assertRaises(ClassroomLimit):
            self.session.advise("x" * 500)
        records = self.session.events(limit=100)
        self.assertEqual([record["sequence"] for record in records], list(range(1, len(records) + 1)))

    def test_close_is_terminal_but_offline_doctor_remains_available(self):
        self.session.control("close")
        with self.assertRaises(ClassroomError):
            self.session.control("resume")
        with self.session.claim_owner():
            self.assertEqual(self.session.run("doctor")["status"], "completed")

    def test_doctor_ignores_revoked_dormant_agent_permissions(self):
        self.session.control("permit", capabilities=["agent", "bash", "skill"])
        self.session.set_setting("tools", ["run_bash"])
        self.session.set_setting("skill", True)
        self.session.control("permit", capabilities=[])
        with self.session.claim_owner():
            result = self.session.run("doctor")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["network_requests"], 0)

    def test_worker_rejects_already_orphaned_parent_before_prctl(self):
        from coursekit.classroom_worker import Cancelled, _parent_death_signal
        with patch("coursekit.classroom_worker.os.getppid", return_value=1), patch("coursekit.classroom_worker.ctypes.CDLL") as libc:
            with self.assertRaises(Cancelled):
                _parent_death_signal(12345)
            libc.assert_not_called()

    def test_normal_worker_uncertainty_closes_gate_and_requires_review(self):
        self.session.set_setting("seed", 105)
        self.session.control("resume")
        with patch("coursekit.classroom.WORKER_FILE", self.fixture_worker), self.session.claim_owner():
            result = self.session.run("complete")
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["cleanup"], "cleanup_unconfirmed")
        self.assertEqual(result["action_outcome"], "outcome_unknown")
        self.assertEqual(self.session.snapshot()["gate"], "paused")
        self.assertTrue(self.session.snapshot()["recovery_required"])
        self.assertIsNone(self.session.snapshot()["active_run"])

    def test_spawn_failure_finalizes_as_not_started(self):
        self.session.control("resume")
        with self.session.claim_owner(), patch("coursekit.classroom.subprocess.Popen", side_effect=OSError("synthetic spawn failure")):
            with self.assertRaises(OSError):
                self.session.run("complete")
        snapshot = self.session.snapshot()
        self.assertIsNone(snapshot["active_run"])
        self.assertEqual(snapshot["last_run"]["status"], "not_started")
        self.assertFalse(snapshot["recovery_required"])

    def test_run_started_journal_failure_does_not_leave_active_run(self):
        self.session.control("resume")
        original = self.session._record

        def failing(event, role, data):
            if event == "run_started":
                raise ClassroomLimit("synthetic journal full")
            return original(event, role, data)

        with self.session.claim_owner(), patch.object(self.session, "_record", side_effect=failing), patch("coursekit.classroom.subprocess.Popen") as popen:
            with self.assertRaises(ClassroomLimit):
                self.session.run("complete")
            popen.assert_not_called()
        self.assertIsNone(self.session.snapshot()["active_run"])
        self.assertEqual(self.session.snapshot()["last_run"]["status"], "not_started")

    def test_callback_failure_after_launch_cancels_and_finalizes(self):
        self.session.set_setting("seed", 101)
        self.session.control("resume")

        def failing(item):
            if item["event"] == "worker_phase":
                raise RuntimeError("synthetic observer failure")

        with patch("coursekit.classroom.WORKER_FILE", self.fixture_worker), self.session.claim_owner():
            with self.assertRaises(RuntimeError):
                self.session.run("complete", on_event=failing)
        snapshot = self.session.snapshot()
        self.assertIsNone(snapshot["active_run"])
        self.assertEqual(snapshot["last_run"]["status"], "outcome_unknown")
        self.assertTrue(snapshot["recovery_required"])
        self.assertEqual(snapshot["gate"], "paused")

    def test_explicit_commands_and_errors_are_observations_not_execution(self):
        original = self.session.snapshot()["prompt"]
        self.session.record_command("set max_tokens 256")
        self.session.record_error("max_tokens must be an integer")
        self.assertEqual(self.session.snapshot()["prompt"], original)
        names = [item["event"] for item in self.session.events(limit=100)]
        self.assertIn("student_command", names)
        self.assertIn("student_error", names)

    def test_ctrl_c_inside_observer_cancels_owned_worker(self):
        self.session.set_setting("seed", 101)
        self.session.control("resume")
        interrupted = []

        def observe(item):
            if item["event"] == "worker_phase" and item["data"]["phase"] == "started" and not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt()

        with patch("coursekit.classroom.WORKER_FILE", self.fixture_worker), self.session.claim_owner():
            result = self.session.run("complete", on_event=observe)
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(self.session.snapshot()["active_run"])
        self.assertEqual(self.session.snapshot()["gate"], "paused")

    def test_stop_request_is_journaled_before_it_is_visible_for_application(self):
        self.session.set_setting("seed", 101)
        self.session.control("resume")
        teacher = Session.open(self.session.id, root=self.root)
        entered, release = threading.Event(), threading.Event()
        original_record = teacher._record
        request_ids = []

        def blocked_record(event, role, data):
            if event == "control_requested" and data["action"] == "stop":
                request_ids.append(data["request_id"])
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("test synchronization timeout")
            return original_record(event, role, data)

        def observe(item):
            if item["event"] == "worker_phase" and item["data"]["phase"] == "started":
                thread = threading.Thread(target=lambda: teacher.control("stop", target_run_id=item["data"]["run_id"]))
                thread.start()
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertFalse(teacher._state()["control_requests"])
                finally:
                    release.set()
                    thread.join(2)

        with patch("coursekit.classroom.WORKER_FILE", self.fixture_worker), patch.object(teacher, "_record", side_effect=blocked_record), self.session.claim_owner():
            result = self.session.run("complete", on_event=observe)
        self.assertEqual(result["status"], "cancelled")
        events = self.session.events(limit=100)
        request = next(item for item in events if item["event"] == "control_requested" and item["data"]["request_id"] == request_ids[0])
        applied = next(item for item in events if item["event"] == "control_applied" and item["data"]["request_id"] == request_ids[0])
        self.assertLess(request["sequence"], applied["sequence"])


if __name__ == "__main__":
    unittest.main()
