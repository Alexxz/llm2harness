import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from coursekit.agent import _digest, run_agent
from coursekit.policy import PolicyError, WorkspacePolicy


class ScriptedClient:
    endpoint = "offline:scripted"

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []
        self.timeout = 60

    def generate(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, "timeout": self.timeout, **kwargs})
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return {"text": response, "metadata": {}}


def action(action_name, **arguments):
    return json.dumps({"action": action_name, "arguments": arguments})


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="course-agent-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.workspace = self.base / "work"
        self.state_path = self.base / "trusted" / "state.json"

    def run_case(self, client, **kwargs):
        return run_agent(client, "Выполни проверяемую задачу", self.workspace, state_path=self.state_path, **kwargs)

    def test_file_feedback_reaches_next_model_call(self):
        client = ScriptedClient(action("write_file", path="note.txt", content="hello"), action("read_file", path="note.txt"), action("finish", answer="Проверено чтением"))
        result = self.run_case(client)
        self.assertEqual(result["status"], "finished")
        self.assertEqual((self.workspace / "note.txt").read_text(), "hello")
        self.assertIn("hello", client.calls[2]["prompt"])
        self.assertEqual(result["steps"], 3)
        self.assertEqual(client.calls[0]["max_tokens"], 2048)
        self.assertTrue(any(e["event"] == "tool_result" for e in result["events"]))

    def test_invalid_responses_consume_budget(self):
        client = ScriptedClient("not JSON")
        result = self.run_case(client, max_steps=1)
        self.assertEqual(result["status"], "step_limit")
        self.assertEqual(result["requests"], 1)
        resumed = ScriptedClient(action("finish", answer="must not be called"))
        result = run_agent(resumed, "", self.workspace, resume_path=self.state_path, max_steps=1)
        self.assertEqual(result["status"], "step_limit")
        self.assertFalse(resumed.calls)

    def test_default_shell_denial_is_observable(self):
        client = ScriptedClient(action("run_bash", script="printf forbidden"), action("finish", answer="Отказ увиден"))
        with patch("coursekit.agent.DockerSandbox") as sandbox:
            result = self.run_case(client)
        sandbox.assert_not_called()
        self.assertTrue(any(e["event"] == "policy_denied" for e in result["events"]))

    def test_path_denial_cannot_write_outside_workspace(self):
        client = ScriptedClient(action("write_file", path="../outside.txt", content="bad"), action("finish", answer="Отказ увиден"))
        result = self.run_case(client)
        self.assertFalse((self.base / "outside.txt").exists())
        self.assertIsNone(result["state"]["pending"])
        self.assertTrue(any(e["event"] == "policy_denied" for e in result["events"]))

    def test_question_pauses_and_answer_resumes_same_run(self):
        first = self.run_case(ScriptedClient(action("ask_user", questions="Q1: какой размер? Рекомендую 8.")))
        self.assertEqual(first["status"], "waiting_for_user")
        resumed_client = ScriptedClient(action("finish", answer="Согласован размер 8"))
        second = run_agent(resumed_client, "", self.workspace, resume_path=self.state_path, answer="Размер 8")
        self.assertEqual(second["status"], "finished")
        self.assertEqual(second["state"]["run_id"], first["state"]["run_id"])
        self.assertEqual(second["steps"], 2)
        self.assertIn("Размер 8", resumed_client.calls[0]["prompt"])
        third_client = ScriptedClient("must not be called")
        third = run_agent(third_client, "", self.workspace, resume_path=self.state_path)
        self.assertEqual(third["status"], "finished")
        self.assertFalse(third_client.calls)

    def test_waiting_resume_without_answer_makes_no_request(self):
        self.run_case(ScriptedClient(action("ask_user", questions="Вопрос?")))
        resumed = ScriptedClient("must not be called")
        result = run_agent(resumed, "", self.workspace, resume_path=self.state_path)
        self.assertEqual(result["status"], "waiting_for_user")
        self.assertFalse(resumed.calls)

    def test_crash_after_write_never_replays_side_effect(self):
        original_write = WorkspacePolicy.write_file

        def crash(policy, path, content):
            original_write(policy, path, content)
            raise KeyboardInterrupt("simulated crash after commit")

        with patch.object(WorkspacePolicy, "write_file", crash), self.assertRaises(KeyboardInterrupt):
            self.run_case(ScriptedClient(action("write_file", path="note", content="once")))
        self.assertEqual((self.workspace / "note").read_text(), "once")
        resumed = ScriptedClient("must not be called")
        result = run_agent(resumed, "", self.workspace, resume_path=self.state_path)
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertTrue(result["state"]["pending"])
        self.assertFalse(resumed.calls)

    def test_resume_cannot_expand_budget_or_change_endpoint(self):
        self.run_case(ScriptedClient(action("ask_user", questions="Вопрос?")), max_steps=3)
        with self.assertRaises(ValueError):
            run_agent(ScriptedClient("unused"), "", self.workspace, resume_path=self.state_path, max_steps=4, answer="да")
        changed = ScriptedClient("unused")
        changed.endpoint = "offline:different"
        with self.assertRaises(ValueError):
            run_agent(changed, "", self.workspace, resume_path=self.state_path, max_steps=3, answer="да")
        self.assertFalse(changed.calls)

    def test_state_inside_workspace_is_rejected(self):
        with self.assertRaises(PolicyError):
            run_agent(ScriptedClient("unused"), "task", self.workspace, state_path=self.workspace / "state.json")

    def test_transport_error_has_no_automatic_retry(self):
        client = ScriptedClient(TimeoutError("synthetic network timeout"), action("finish", answer="unused"))
        result = self.run_case(client)
        self.assertEqual(result["status"], "transport_error")
        self.assertEqual(len(client.calls), 1)
        self.assertIsNone(result["state"]["pending"])

    def test_deadline_clips_request_timeout_and_blocks_late_action(self):
        client = ScriptedClient(action("finish", answer="done"))
        result = self.run_case(client, deadline_seconds=0.5)
        self.assertEqual(result["status"], "finished")
        self.assertLessEqual(client.calls[0]["timeout"], 0.5)
        self.assertEqual(client.timeout, 60)

        second_state = self.base / "trusted" / "late.json"

        class SlowClient(ScriptedClient):
            def generate(self, prompt, **kwargs):
                time.sleep(0.03)
                return super().generate(prompt, **kwargs)

        late = run_agent(SlowClient(action("write_file", path="late", content="no")), "task", self.workspace, state_path=second_state, deadline_seconds=0.01)
        self.assertEqual(late["status"], "deadline")
        self.assertFalse((self.workspace / "late").exists())

    def test_duplicate_json_fields_are_rejected(self):
        client = ScriptedClient('{"action":"finish","action":"write_file","arguments":{"path":"x","content":"y"}}')
        result = self.run_case(client, max_steps=1)
        self.assertEqual(result["status"], "step_limit")
        self.assertFalse((self.workspace / "x").exists())

    def test_control_only_interview_has_no_file_permissions(self):
        result = self.run_case(ScriptedClient(action("ask_user", questions="Какие правила игры?")), allowed_tools={"ask_user", "finish"})
        self.assertEqual(result["status"], "waiting_for_user")
        self.assertEqual(result["state"]["config"]["allowed_tools"], [])

    def test_mcp_is_discovered_before_first_model_request(self):
        self.workspace.mkdir()
        (self.workspace / "notes.txt").write_text("synthetic stock: 7")
        client = ScriptedClient(action("mcp_call", name="read_note", arguments={"path": "notes.txt"}), action("finish", answer="Прочитано"))
        result = self.run_case(client, mcp=True, allowed_tools={"ask_user", "finish"})
        self.assertEqual(result["status"], "finished")
        self.assertIn("read_note", client.calls[0]["prompt"])
        self.assertIn("inputSchema", client.calls[0]["prompt"])
        self.assertIn("synthetic stock: 7", client.calls[1]["prompt"])
        self.assertEqual(result["state"]["config"]["allowed_tools"], ["mcp_call"])

    def test_decoding_settings_are_forwarded_and_bound_on_resume(self):
        client = ScriptedClient(action("ask_user", questions="Продолжать?"))
        self.run_case(client, temperature=0.7, seed=123)
        self.assertEqual(client.calls[0]["temperature"], 0.7)
        self.assertEqual(client.calls[0]["seed"], 123)
        with self.assertRaises(ValueError):
            run_agent(ScriptedClient("unused"), "", self.workspace, resume_path=self.state_path, answer="да")

    def test_old_schema_one_checkpoint_defaults_migrate_without_budget_reset(self):
        self.run_case(ScriptedClient(action("ask_user", questions="Продолжать?")))
        state = json.loads(self.state_path.read_text())
        state["config"].pop("temperature")
        state["config"].pop("seed")
        state["config_digest"] = _digest(state["config"])
        self.state_path.write_text(json.dumps(state))
        client = ScriptedClient(action("finish", answer="Готово"))
        result = run_agent(client, "", self.workspace, resume_path=self.state_path, answer="да")
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["steps"], 2)
        self.assertEqual(result["state"]["config"]["temperature"], 0)
        self.assertEqual(result["state"]["config"]["seed"], 42)


if __name__ == "__main__":
    unittest.main()
