import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from coursekit.policy import PolicyError
from coursekit.sandbox import Captured, DockerSandbox, SandboxError, _snapshot_from_tar


def archive(files, *, symlink=None):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as stream:
        for name, content in files.items():
            data = content.encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            stream.addfile(member, io.BytesIO(data))
        if symlink:
            member = tarfile.TarInfo(symlink)
            member.type = tarfile.SYMTYPE
            member.linkname = "outside"
            stream.addfile(member)
    return buffer.getvalue()


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="course-sandbox-test-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "work"
        self.workspace.mkdir()
        self.commands = []
        self.export = archive({"hello.sh": "printf 'hello\\n'\n"})
        self.execution = Captured(0, b"hello\n", b"")
        self.cleanup_result = Captured(0, b"removed", b"")

    def capture(self, command, timeout, **kwargs):
        args = command[command.index("unix:///var/run/docker.sock") + 1:]
        self.commands.append(args)
        self.assertGreater(timeout, 0)
        if args[:2] == ["image", "inspect"]:
            return Captured(0, ("sha256:" + "a" * 64 + "\n").encode(), b"")
        if args[0] == "exec" and args[-1] == "printf example":
            return self.execution
        if args[0] == "cp":
            return Captured(0, self.export, b"")
        if args[0] == "rm":
            return self.cleanup_result
        return Captured(0, b"ok\n", b"")

    def run_sandbox(self):
        with patch("coursekit.sandbox.shutil.which", return_value="/usr/bin/docker"), patch("coursekit.sandbox._capture", side_effect=self.capture):
            return DockerSandbox().run(self.workspace, "printf example")

    def test_read_only_input_tmpfs_and_checked_export(self):
        result = self.run_sandbox()
        self.assertEqual(result["stdout"], "hello\n")
        self.assertTrue(result["snapshot_applied"])
        self.assertEqual(result["changed_files"], ["hello.sh"])
        self.assertEqual((self.workspace / "hello.sh").read_text(), "printf 'hello\\n'\n")
        run = next(command for command in self.commands if command[0] == "run")
        self.assertIn("--read-only", run)
        self.assertEqual(run[run.index("--network") + 1], "none")
        self.assertEqual(run[run.index("--pull") + 1], "never")
        self.assertIn("readonly", run[run.index("--mount") + 1])
        self.assertTrue(any(value.startswith("/work:rw,") for value in run))
        self.assertNotIn("--privileged", run)
        self.assertEqual(self.commands[-1][:2], ["rm", "--force"])

    def test_timeout_discards_output_snapshot_and_removes_container(self):
        self.execution = Captured(None, b"partial", b"", timed_out=True)
        result = self.run_sandbox()
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["snapshot_applied"])
        self.assertFalse(any(command[0] == "cp" for command in self.commands))
        self.assertEqual(self.commands[-1][:2], ["rm", "--force"])
        self.assertFalse((self.workspace / "hello.sh").exists())

    def test_output_limit_stops_without_applying_files(self):
        self.execution = Captured(-9, b"bounded", b"", output_limit=True)
        result = self.run_sandbox()
        self.assertTrue(result["output_limit"])
        self.assertFalse(result["snapshot_applied"])

    def test_links_and_traversal_exports_are_rejected(self):
        for payload in (archive({}, symlink="link"), archive({"../outside": "bad"}), archive({"/absolute": "bad"})):
            with self.assertRaises(PolicyError):
                _snapshot_from_tar(payload)

    def test_openspec_metadata_export_obeys_narrow_allowance(self):
        name = "openspec/changes/add-snake/.openspec.yaml"
        self.assertEqual(_snapshot_from_tar(archive({name: "schema: spec-driven\n"}))[name], b"schema: spec-driven\n")
        with self.assertRaises(PolicyError):
            _snapshot_from_tar(archive({"openspec/changes/add-snake/.other": "no"}))

    def test_deletion_of_original_file_fails_closed(self):
        (self.workspace / "original").write_text("keep")
        self.export = archive({"hello.sh": "not applied"})
        with self.assertRaises(PolicyError):
            self.run_sandbox()
        self.assertEqual((self.workspace / "original").read_text(), "keep")
        self.assertFalse((self.workspace / "hello.sh").exists())

    def test_missing_docker_never_executes_host_fallback(self):
        with patch("coursekit.sandbox.shutil.which", return_value=None), patch("coursekit.sandbox._capture") as capture:
            with self.assertRaises(SandboxError):
                DockerSandbox().run(self.workspace, "anything")
            capture.assert_not_called()

    def test_cleanup_failure_is_uncertain(self):
        self.cleanup_result = Captured(1, b"", b"daemon unavailable")
        with self.assertRaises(SandboxError) as caught:
            self.run_sandbox()
        self.assertTrue(caught.exception.outcome_unknown)


if __name__ == "__main__":
    unittest.main()
