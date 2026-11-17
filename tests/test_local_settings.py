import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coursekit.local_settings import load_local_settings


class LocalSettingsTests(unittest.TestCase):
    def test_absent_file_preserves_environment(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'COURSE_TEACHER_BACKEND': 'command'}, clear=True):
            load_local_settings(root)
            self.assertEqual(os.environ['COURSE_TEACHER_BACKEND'], 'command')

    def test_file_overrides_stale_shell_and_preserves_other_variables(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'COURSE_TEACHER_BACKEND': 'command', 'KEEP': 'value'}, clear=True):
            path = Path(root) / '.local'
            path.mkdir()
            (path / 'teacher.json').write_text(json.dumps({'backend': 'codex', 'command': '/tmp/example-launcher'}))
            load_local_settings(root)
            self.assertEqual(os.environ['COURSE_TEACHER_BACKEND'], 'codex')
            self.assertEqual(os.environ['COURSE_TEACHER_COMMAND'], '/tmp/example-launcher')
            self.assertEqual(os.environ['KEEP'], 'value')

    def test_invalid_file_does_not_partially_apply_or_expose_values(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            path = Path(root) / '.local'
            path.mkdir()
            for value in [{'backend': 'codex', 'unexpected': 'private-marker'}, {'backend': 'unknown'}, {'command': 3}]:
                (path / 'teacher.json').write_text(json.dumps(value))
                with self.assertRaises(ValueError) as error:
                    load_local_settings(root)
                self.assertNotIn('private-marker', str(error.exception))
                self.assertNotIn('COURSE_TEACHER_BACKEND', os.environ)
