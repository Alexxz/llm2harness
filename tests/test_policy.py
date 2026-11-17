import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coursekit.policy import PolicyError, WorkspacePolicy, validate_action


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="course-policy-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.policy = WorkspacePolicy(self.base / "work")

    def test_read_write_and_snapshot(self):
        self.policy.write_file("notes/task.txt", "проверка\n")
        self.assertEqual(self.policy.read_file("notes/task.txt"), "проверка\n")
        self.assertEqual(self.policy.snapshot(), {"notes/task.txt": "проверка\n".encode()})

    def test_traversal_absolute_and_hidden_denied_before_creation(self):
        for name in ("../outside.txt", str(self.base / "outside.txt"), "a/../b", "a//b", ".hidden", "x/.hidden"):
            with self.subTest(name=name), self.assertRaises(PolicyError):
                self.policy.write_file(name, "x")
        self.assertFalse((self.base / "outside.txt").exists())

    def test_symlink_file_and_directory_escape_denied(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "note.txt").write_text("original")
        (self.policy.root / "linked").symlink_to(outside, target_is_directory=True)
        (self.policy.root / "linked-file").symlink_to(outside / "note.txt")
        for name in ("linked/note.txt", "linked-file"):
            with self.assertRaises(PolicyError):
                self.policy.read_file(name)
            with self.assertRaises(PolicyError):
                self.policy.write_file(name, "changed")
        self.assertEqual((outside / "note.txt").read_text(), "original")

    def test_hardlink_cannot_modify_external_file(self):
        outside = self.base / "outside.txt"
        outside.write_text("original")
        os.link(outside, self.policy.root / "linked")
        with self.assertRaises(PolicyError):
            self.policy.write_file("linked", "changed")
        with self.assertRaises(PolicyError):
            self.policy.read_file("linked")
        self.assertEqual(outside.read_text(), "original")

    def test_total_byte_cap_applies_to_direct_writes_and_replacements(self):
        with patch("coursekit.policy.MAX_WORKSPACE_BYTES", 8):
            self.policy.write_file("one", "1234")
            self.policy.write_file("two", "5678")
            self.policy.write_file("one", "12")
            with self.assertRaises(PolicyError):
                self.policy.write_file("three", "123")
            self.assertFalse((self.policy.root / "three").exists())

    def test_file_count_cap_applies_to_direct_writes(self):
        with patch("coursekit.policy.MAX_FILES", 2):
            self.policy.write_file("one", "1")
            self.policy.write_file("two", "2")
            self.policy.write_file("two", "replacement")
            with self.assertRaises(PolicyError):
                self.policy.write_file("three", "3")

    def test_trusted_source_workspace_is_rejected(self):
        with self.assertRaises(PolicyError):
            WorkspacePolicy(Path(__file__).resolve().parent.parent)

    def test_action_exact_fields_and_default_permissions(self):
        with self.assertRaises(PolicyError):
            validate_action({"action": "run_bash", "arguments": {"script": "printf hi"}})
        with self.assertRaises(PolicyError):
            validate_action({"action": "read_file", "arguments": {"path": "x", "extra": "ignored"}})
        validate_action({"action": "ask_user", "arguments": {"questions": "Какой вариант?"}})

    def test_controls_can_be_named_without_granting_tools(self):
        validate_action({"action": "ask_user", "arguments": {"questions": "Вопрос?"}}, ["ask_user", "finish"])
        with self.assertRaises(PolicyError):
            validate_action({"action": "read_file", "arguments": {"path": "x"}}, ["ask_user", "finish"])

    def test_only_exact_openspec_metadata_hidden_path_is_allowed(self):
        name = "openspec/changes/add-snake/.openspec.yaml"
        self.policy.write_file(name, "schema: spec-driven\n")
        self.assertEqual(self.policy.read_file(name), "schema: spec-driven\n")
        self.assertIn(name, self.policy.snapshot())
        for invalid in (".openspec.yaml", "openspec/other/add-snake/.openspec.yaml", "openspec/changes/add-snake/.other", "openspec/changes/.hidden/.openspec.yaml"):
            with self.subTest(path=invalid), self.assertRaises(PolicyError):
                self.policy.write_file(invalid, "no")


if __name__ == "__main__":
    unittest.main()
