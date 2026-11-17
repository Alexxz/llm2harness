import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from coursekit.classroom import Session
from coursekit.workbench import Workspace, advance_phase, parse_pairs, teacher_view

ROOT = Path(__file__).resolve().parents[1]


class WorkspaceTests(unittest.TestCase):
    def make(self, lesson):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        directory = next((ROOT/'lessons').glob(lesson+'-*'))
        shutil.copytree(directory, root/'lessons'/directory.name)
        session = Session.create(lesson, root=root)
        session.control('resume')
        return session, Workspace(session)

    def wait(self, lab):
        lab.thread.join(3)
        self.assertFalse(lab.busy)
        self.assertFalse(lab.error, lab.error)

    def test_presets_never_execute_and_phase_restricts_parameters(self):
        session, lab = self.make('01')
        with patch.object(session, 'run') as run:
            lab.load('baseline')
            run.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'откроется'):
                lab.load('variation')
            lab.state['parameters']['temperature'] = .9
            with self.assertRaisesRegex(ValueError, 'закрыто'):
                lab.start()
            run.assert_not_called()
            advance_phase(session)
            lab.load('variation')
            advance_phase(session)
            lab.load('length')
            self.assertEqual(lab.state['parameters']['max_tokens'], 8)

    def test_actual_input_and_result_are_in_main_session(self):
        session, lab = self.make('01')
        lab.load('baseline')
        prompt = lab.state['prompt'] + '\nmy edit'
        lab.state['prompt'] = prompt
        result = {'status':'completed', 'run_id':'a'*32, 'result':{'text':'real worker result'}}
        with patch.object(session, 'run', return_value=result) as run:
            lab.start()
            self.wait(lab)
            self.assertEqual(run.call_args.args, ('complete',))
            self.assertEqual(run.call_args.kwargs['input_text'], prompt)
        self.assertEqual(session.snapshot()['prompt'], prompt)
        self.assertEqual(teacher_view(session)['output'], 'real worker result')
        states = [e for e in session.events(limit=100) if e['event']=='workspace_state']
        self.assertEqual(states[-1]['data']['state']['prompt'], prompt)

    def test_training_registers_adapter_only_on_success_and_preserves_request(self):
        session, lab = self.make('04')
        lab.load('baseline')
        original = lab.state['prompt']
        with self.assertRaisesRegex(ValueError, 'закрыто'):
            lab.switch_mode()
        advance_phase(session)
        lab.switch_mode()
        def train(kind, input_text=None, on_event=None):
            data = json.loads(input_text)
            self.assertEqual(data['operation'], 'train')
            return {'status':'completed', 'result':{'completed':True,'steps':12,'pairs':data['config']['pairs'], 'adapter_id':data['adapter_id'], 'parameter_digest_before':'a','parameter_digest_after':'b'}}
        with patch.object(session, 'run', side_effect=train):
            lab.start()
            self.wait(lab)
        self.assertEqual(lab.state['model'], 'original')
        lab.switch_mode()
        lab.switch_model()
        pairs_before = lab.state['adapters'][0]['pairs']
        lab.state['pairs'] = 'changed -> sample'
        def infer(kind, input_text=None, on_event=None):
            data = json.loads(input_text)
            self.assertEqual(data['operation'], 'after')
            self.assertEqual(data['config']['raw_prompt'], original)
            self.assertEqual(data['config']['pairs'], pairs_before)
            return {'status':'completed','run_id':'b'*32,'result':{'text':'{"drink":"tea","cups":2}'}}
        with patch.object(session, 'run', side_effect=infer):
            lab.start()
            self.wait(lab)
        lab.switch_model()
        self.assertEqual(lab.state['model'], 'original')
        self.assertEqual(lab.state['prompt'], original)
        self.assertTrue(lab.state['check']['content_correct'])

    def test_cancelled_training_does_not_publish_adapter(self):
        session, lab = self.make('04')
        lab.load('baseline')
        advance_phase(session)
        lab.switch_mode()
        with patch.object(session,'run',return_value={'status':'cancelled'}):
            lab.start()
            lab.thread.join(3)
        self.assertTrue(lab.error)
        self.assertEqual(lab.state['adapters'], [])
        session.control('pause')
        with self.assertRaisesRegex(ValueError,'приостановлен'):
            lab.start()

    def test_pair_parser_does_not_interpret_output(self):
        pairs = parse_pairs('two teas -> {"cups":2}\n\ncoffee -> answer -> tail')
        self.assertEqual(pairs[1]['answer'], 'answer -> tail')
        with self.assertRaises(ValueError):
            parse_pairs('missing separator')
        with self.assertRaises(ValueError):
            parse_pairs(' -> missing input')

    def test_all_four_lessons_have_consistent_phase_presets(self):
        for lesson in ('00','01','02','03','04'):
            session, lab = self.make(lesson)
            ids = {p['id'] for p in lab.spec['presets']}
            for phase in lab.spec['phases']:
                self.assertTrue(set(phase['presets']) <= ids)
                self.assertTrue(phase['instruction'])
            for preset in lab.spec['presets']:
                self.assertTrue(session._asset(preset['prompt_file']).strip())

    def test_teacher_advances_only_after_experience_question_and_answer(self):
        import time
        from coursekit.teacher import TeacherRuntime
        session, lab = self.make('01')
        class Teacher:
            def answer(self, events):
                return {'reply':'Первый опыт обсудили.', 'note':'Ответ проверен.', 'control':'advance'}
            def close(self):
                pass
        runtime = TeacherRuntime(session, Teacher())
        def wait(predicate):
            deadline = time.monotonic()+3
            while time.monotonic()<deadline:
                if predicate(): return
                time.sleep(.02)
            self.fail('Teacher did not reach expected stage')
        with patch.object(session, 'run') as run:
            runtime.start()
            try:
                wait(lambda: teacher_view(session)['teaching']['introduced'])
                lab.state.update(preset='baseline',result={'text':'author fixture'},error='')
                lab.save('completed');lab.save('completed')
                wait(lambda: teacher_view(session)['teaching']['question_asked'])
                session.ask('Модель дописала продолжение, два ответа совпали.')
                wait(lambda: teacher_view(session)['phase_index']==1)
                run.assert_not_called()
            finally:
                runtime.close()

    def test_intro_lesson_loads_and_advances_without_model_permission(self):
        session, lab = self.make('00')
        lab.load('baseline')
        advance_phase(session)
        with patch.object(session, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'приостановлен'):
                lab.start()
            run.assert_not_called()
        self.assertFalse(lab.phase.get('requests',True))

    def test_mode_switch_keeps_model_output_separate_from_training_report(self):
        session, lab = self.make('04')
        advance_phase(session)
        lab.state.update(output='Настоящий ответ модели', result={'text':'Настоящий ответ модели'})
        lab.switch_mode()
        self.assertEqual(lab.state['output'],'')
        lab.state.update(output='Добавка обучена', result={'completed':True})
        lab.switch_mode()
        self.assertEqual(lab.state['output'],'Настоящий ответ модели')
        lab.switch_mode()
        self.assertEqual(lab.state['output'],'Добавка обучена')
        with self.assertRaisesRegex(ValueError,'режиме «Запрос»'):
            lab.switch_model()

    def test_stop_works_with_open_picker_or_help(self):
        import curses
        from coursekit.workbench import _screen
        for panel_key in (curses.KEY_F4, curses.KEY_F1):
            session, lab = self.make('01')
            class Screen:
                def timeout(self, value): pass
                def getmaxyx(self): return (26,60)
                def erase(self): pass
                def addnstr(self, *args): pass
                def move(self, *args): pass
                def refresh(self): pass
                def get_wch(self):
                    key = next(self.keys)
                    if key == 'close':
                        session.control('close')
                        return curses.KEY_RESIZE
                    return key
            screen=Screen()
            screen.keys=iter((panel_key,curses.KEY_F6,'close'))
            with patch.object(lab,'cancel') as cancel, patch('curses.curs_set'):
                _screen(screen,lab)
                cancel.assert_called_once()
            self.assertEqual(teacher_view(session)['interface']['panel'], 'presets' if panel_key==curses.KEY_F4 else 'help')

    def test_preset_replaces_draft_parameters_and_clears_output_without_running(self):
        session,lab=self.make('00')
        lab.state.update(prompt='arbitrary draft',output='old actual response',result={'text':'old actual response'},check={'json_valid':True},last_input={'prompt':'old'},error='old error')
        lab.state['parameters']={'max_tokens':17,'temperature':1,'seed':8}
        lab.state['results_by_mode']={'request':{'output':'old cached response'}}
        with patch.object(session,'run') as run:
            lab.load('colors')
            run.assert_not_called()
        self.assertEqual(lab.state['prompt'],session._asset('prompts/colors.txt'))
        self.assertEqual(lab.state['parameters'],next(p['parameters'] for p in lab.spec['presets'] if p['id']=='colors'))
        self.assertEqual(lab.state['output'],'')
        self.assertIsNone(lab.state['result'])
        self.assertIsNone(lab.state['check'])
        self.assertIsNone(lab.state['last_input'])
        self.assertEqual(lab.state['error'],'')
        self.assertNotIn('request',lab.state['results_by_mode'])

    def test_intro_first_request_is_available_only_in_last_phase(self):
        session,lab=self.make('00')
        lab.load('animals')
        with patch.object(session,'run',return_value={'status':'completed','run_id':'c'*32,'result':{'text':'тигр'}}) as run:
            with self.assertRaisesRegex(ValueError,'приостановлен'):lab.start()
            for _ in range(2):advance_phase(session)
            lab.start();self.wait(lab)
            self.assertEqual(run.call_args.args,('complete',))
            self.assertEqual(lab.state['output'],'тигр')
            lab.load('counting')
            self.assertEqual(lab.state['output'],'')
