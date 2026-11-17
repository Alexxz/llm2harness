"""Startup failures must not create sessions or replace an unrelated model."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from coursekit.preparation import ensure_base, prepare
from coursekit.classroom_console import main

ROOT = Path(__file__).resolve().parents[1]


class PreparationTests(unittest.TestCase):
    def fixture(self, root, lesson='01'):
        directory = root / ('lessons/' + lesson + '-test')
        directory.mkdir(parents=True)
        (directory / 'lesson.json').write_text(json.dumps({'id': lesson}))
        (root / 'resources').mkdir(exist_ok=True)
        (root / 'resources/models.json').write_text((ROOT / 'resources/models.json').read_text())

    def test_failure_prevents_session_creation(self):
        with patch('coursekit.preparation.prepare', side_effect=ValueError('not ready')), patch('coursekit.classroom.Session.create') as create, patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(['start', '--lesson', '01']), 2)
            create.assert_not_called()

    def test_interface_needs_teacher_but_not_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, '00')
            with patch('coursekit.preparation.check_teacher') as teacher, patch('coursekit.preparation.ensure_base') as model, patch('sys.stdout', new_callable=io.StringIO):
                prepare(root, '00')
            teacher.assert_called_once_with(root)
            model.assert_not_called()
            self.assertFalse((root / 'results/classroom').exists())
            self.assertTrue(json.loads(next((root / 'results/preparation').glob('00-*/preflight.json')).read_text())['ok'])

    def test_teacher_failure_is_recorded_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            with patch('coursekit.preparation.check_teacher', side_effect=RuntimeError('unavailable')), patch('coursekit.preparation.ensure_base') as model, patch('sys.stdout', new_callable=io.StringIO):
                with self.assertRaises(RuntimeError):
                    prepare(root, '01')
            model.assert_not_called()
            report = json.loads(next((root / 'results/preparation').glob('01-*/preflight.json')).read_text())
            self.assertFalse(report['ok'])
            self.assertNotIn('unavailable', json.dumps(report))

    def test_training_endpoint_rejected_before_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, '04')
            with patch('coursekit.preparation.check_teacher'), patch('coursekit.preparation.subprocess.run') as install, patch('sys.stdout', new_callable=io.StringIO):
                with self.assertRaisesRegex(ValueError, '8089'):
                    prepare(root, '04', endpoint='http://127.0.0.1:8090')
            install.assert_not_called()

    def test_training_preparation_checks_real_response_without_training(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, '04')
            with patch('coursekit.preparation.check_teacher'), patch('coursekit.preparation.subprocess.run') as install, patch('coursekit.learning_lab.ensure_prepared'), patch('coursekit.training_lesson.ensure_server') as server, patch('coursekit.training_lesson.profile', return_value={'model': 'training'}), patch('coursekit.preparation.ModelClient') as client, patch('sys.stdout', new_callable=io.StringIO):
                client.return_value.generate.return_value = {'text': 'Hello'}
                prepare(root, '04', endpoint='http://127.0.0.1:8089')
            install.assert_called_once()
            self.assertIn('prepare_training.py', install.call_args.args[0][1])
            server.assert_called_once_with(root)
            client.return_value.generate.assert_called_once()
            report = json.loads(next((root / 'results/preparation').glob('04-*/preflight.json')).read_text())
            self.assertTrue(report['ok'])
            self.assertFalse((root / 'results/classroom').exists())

    def test_other_model_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            with patch('coursekit.preparation.available', return_value=False), patch('coursekit.preparation.props', return_value={'model_path': '/other.gguf'}), patch('coursekit.preparation.subprocess.Popen') as launch:
                with self.assertRaisesRegex(ValueError, 'другой'):
                    ensure_base(root, 'http://127.0.0.1:8088', root)
            launch.assert_not_called()

    def test_reuse_requires_real_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            with patch('coursekit.preparation.available', return_value=False), patch('coursekit.preparation.props', return_value={'model_path': 'qwen2.5-1.5b-base-q4_k_m.gguf'}), patch('coursekit.preparation.ModelClient') as client:
                client.return_value.generate.return_value = {'text': 'кот', 'metadata': {}}
                self.assertIsNone(ensure_base(root, 'http://127.0.0.1:8088', root))
            client.return_value.generate.assert_called_once()
            report = json.loads((root / 'model-check.json').read_text())
            self.assertIn('cat', report['prompt'])

    def test_owned_server_stops_on_failed_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            profile = json.loads((root / 'resources/models.json').read_text())
            profile['base'].update(bytes=1, sha256=__import__('hashlib').sha256(b'x').hexdigest())
            (root / 'resources/models.json').write_text(json.dumps(profile))
            binary = root / 'runtime/llama-b12380/llama-server'
            binary.parent.mkdir(parents=True)
            binary.touch()
            (root / 'models').mkdir()
            (root / 'models' / profile['base']['local_name']).write_bytes(b'x')
            process = Mock(pid=12345)
            process.poll.return_value = None
            with patch('coursekit.preparation.available', return_value=True), patch('coursekit.preparation.subprocess.Popen', return_value=process), patch('coursekit.preparation.props', side_effect=OSError), patch('sys.stdout', new_callable=io.StringIO):
                with self.assertRaises(ValueError):
                    ensure_base(root, 'http://127.0.0.1:8088', root, timeout=0)
            process.terminate.assert_called_once()
            process.wait.assert_called_once()


if __name__ == '__main__':
    unittest.main()
