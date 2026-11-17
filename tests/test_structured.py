import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from coursekit.classroom import Session
from coursekit.classroom_console import StudentConsole
from coursekit.structured import evaluate, expected_order, check, compare

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT/'lessons/03-structured-prompts/core-schema.json').read_text())
PROMPT = (ROOT/'lessons/03-structured-prompts/prompts/baseline.txt').read_text()

class StructuredTests(unittest.TestCase):
    def test_three_levels_and_no_silent_repair(self):
        for text, expected in [
            ('Вот JSON: {"drink":"tea","cups":2}', (False,None,None)),
            ('```json\n{"drink":"tea","cups":2}\n```', (False,None,None)),
            ('{"drink":"tea","cups":"2"}', (True,False,None)),
            ('{"drink":"tea","cups":true}', (True,False,None)),
            ('{"drink":"coffee","cups":2}', (True,True,False)),
            ('{ "cups": 2, "drink": "tea" }', (True,True,True)),
            ('{"drink":"tea","cups":2,"extra":0}', (True,False,None)),
            ('{"drink":"tea","cups":NaN}', (False,None,None)),
            ('{"drink":"tea","drink":"coffee","cups":2}', (False,None,None)),
        ]:
            with self.subTest(text=text):
                report=evaluate(text,PROMPT,SCHEMA)
                self.assertEqual(tuple(report[k] for k in ('json_valid','schema_valid','content_correct')),expected)

    def test_unknown_semantics_are_not_a_failure(self):
        prompt=PROMPT.replace('Две чашки чая.','Пожалуйста, принесите что-нибудь вкусное.')
        report=evaluate('{"drink":"tea","cups":2}',prompt,SCHEMA)
        self.assertTrue(report['schema_valid']);self.assertIsNone(report['content_correct'])
        self.assertEqual(expected_order(PROMPT.replace('Две чашки чая.','Пять чашек кофе.')),{'drink':'coffee','cups':5})

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        shutil.copytree(ROOT/'lessons/03-structured-prompts',self.root/'lessons/03-structured-prompts')
        self.session=Session.create('03',root=self.root)

    def add_run(self, mode, index, output='{"drink":"tea","cups":2}', **changes):
        run_id=f'{index:032x}'
        prompt=(self.root/'lessons/03-structured-prompts/prompts'/f'{mode}.txt').read_text()
        settings=dict(self.session.snapshot()['settings']);settings.update(changes)
        run=self.session.directory/'runs'/run_id;run.mkdir()
        (run/'request.json').write_text(json.dumps({'kind':'complete','prompt':prompt,'settings':settings,'schema':None,'endpoint':'http://127.0.0.1:8088'}))
        result={'status':'completed','result':{'text':output,'metadata':{'server':{'model':'local-test-model'}}}}
        (run/'result.json').write_text(json.dumps(result))
        with self.session._lock('metadata.lock'):
            state=self.session._state();state['last_run']={'id':run_id};self.session._save(state)
        self.session._record('run_completed','system',{'run_id':run_id,'kind':'complete'})
        return run

    def test_check_uses_original_input_even_after_edit_and_never_calls_model(self):
        run=self.add_run('baseline',1)
        before=(run/'result.json').read_bytes()
        self.session.set_prompt(PROMPT.replace('Две','Три'))
        output=[]
        with patch.object(self.session,'run',side_effect=AssertionError('no model')):
            StudentConsole(self.session,output=lambda value,**kw:output.append(value)).execute('check')
        self.assertIn('Сведения совпадают с заявкой: да',output[0])
        self.assertEqual((run/'result.json').read_bytes(),before)

    def test_compare_reports_different_settings_and_preserves_full_answers(self):
        self.add_run('baseline',1,output='raw baseline')
        self.add_run('variation',2,output='raw variation',temperature=1)
        output=compare(self.session)
        self.assertIn('настройки различаются',output)
        self.assertIn('raw baseline',output);self.assertIn('raw variation',output)
        event=[e for e in self.session.events(limit=100) if e['event']=='structured_compared'][-1]
        self.assertFalse(event['data']['comparable'])

    def test_comparable_pair_and_missing_case(self):
        self.add_run('baseline',1)
        with self.assertRaisesRegex(ValueError,'оба завершённых'):compare(self.session)
        self.add_run('variation',2)
        self.assertIn('настройки совпадают',compare(self.session))

    def test_initial_profile_is_local_unconstrained_and_fixed(self):
        state=self.session.snapshot()
        self.assertEqual(state['endpoint'],'http://127.0.0.1:8088')
        self.assertIsNone(state['schema'])
        self.assertEqual([state['settings'][k] for k in ('temperature','seed','max_tokens')],[0,42,96])

    def test_checks_give_next_step_without_overwriting_an_existing_draft(self):
        self.session.control('resume')
        self.add_run('baseline',1)
        self.assertIn('load variation',check(self.session))
        self.session.load_prompt('variation')
        self.add_run('variation',2)
        self.assertIn('Введи compare',check(self.session))
        self.session.set_prompt('My next draft')
        self.assertIn('Следующий вход уже изменён',check(self.session))
        self.assertEqual(self.session.snapshot()['prompt'],'My next draft')
