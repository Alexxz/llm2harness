import copy
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from coursekit.classroom import Session
from coursekit.learning_lab import LearningLab,content,execute
from coursekit.teacher import TeacherRuntime

ROOT=Path(__file__).resolve().parents[1]

class LearningLabTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        shutil.copytree(ROOT/'lessons/04-training-data',self.root/'lessons/04-training-data')
        self.parent=Session.create('04',root=self.root);self.parent.control('resume')
        self.lab=LearningLab(self.parent);self.addCleanup(self.lab.close)

    def wait(self):
        self.lab.thread.join(3);self.assertFalse(self.lab.busy)
        self.assertFalse(self.lab.error,self.lab.error)

    def fake_run(self,kind,on_event=None):
        data=json.loads(self.lab.child.snapshot()['prompt']);operation=data['operation']
        if operation=='train':
            if on_event:on_event({'event':'learning_progress','data':{'step':12,'total':12,'loss':.1}})
            value={'completed':True,'steps':12,'training_seconds':1,'parameter_digest_before':'before','parameter_digest_after':'after','adapter_id':data['adapter_id'],'trainable_parameters':12}
        else:
            req=data['config']['new_request'] if operation=='probe' else data['config']['request']
            value={'text':'{"drink":"tea","cups":2}','request':req,'prompt':'same exact prompt' if operation!='probe' else 'new prompt','settings':{'seed':42},'adapter':operation in {'after','probe'},'adapter_id':data['adapter_id']}
        return {'status':'completed','result':value}

    def test_all_transitions_are_explicit_and_do_not_claim_understanding(self):
        with patch.object(self.lab.child,'run',side_effect=self.fake_run) as run:
            self.lab.advance();self.wait();self.assertEqual(self.lab.state['stage'],1)
            self.lab.advance();self.assertEqual(run.call_count,1)
            self.assertIsNone(self.lab.state['training'])
            self.lab.advance();self.wait();self.assertTrue(self.lab.state['training']['completed'])
            self.lab.advance();self.wait()
            before,after=self.lab.state['before'],self.lab.state['after']
            self.assertEqual(before['prompt'],after['prompt']);self.assertFalse(before['adapter']);self.assertTrue(after['adapter'])
            self.lab.start('off');self.wait();self.assertFalse(self.lab.state['off']['adapter'])
            self.lab.advance();self.assertEqual(self.lab.state['stage'],5)
            self.lab.advance();self.wait();self.assertEqual(self.lab.state['stage'],6)
            self.assertEqual(self.lab.advance(),'close')
            event=[e for e in self.parent.events(limit=100) if e['event']=='learning_lab_finished'][-1]
            self.assertTrue(event['data']['understanding_not_assessed'])

    def test_pair_editing_freezes_before_training_and_survives_reopening(self):
        with patch.object(self.lab.child,'run',side_effect=self.fake_run):
            self.lab.advance();self.wait()
        self.lab.edit('{"drink":"tea","cups":"2"}')
        self.lab.advance()
        with self.assertRaises(ValueError):self.lab.edit('changed silently')
        child=self.lab.child.id;self.lab.close()
        reopened=LearningLab(self.parent)
        try:
            self.assertEqual(reopened.child.id,child);self.assertEqual(reopened.state['stage'],2)
            self.assertIn('"2"',reopened.state['config']['pairs'][0]['answer'])
        finally:reopened.close()

    def test_closed_gate_and_failed_training_cannot_advance(self):
        self.parent.control('pause')
        with self.assertRaisesRegex(ValueError,'приостановлен'):self.lab.advance()
        self.parent.control('resume');self.lab.state['stage']=2
        with patch.object(self.lab.child,'run',return_value={'status':'cancelled'}):
            self.lab.advance();self.lab.thread.join(3)
        self.assertEqual(self.lab.state['stage'],2);self.assertIsNone(self.lab.state['training'])
        with self.assertRaisesRegex(ValueError,'обучения ещё нет'):self.lab.start('after')

    def test_parent_pause_cancels_child_operation(self):
        started=threading.Event();release=threading.Event()
        def slow(*a,**kw):started.set();release.wait(2);return {'status':'cancelled'}
        with patch.object(self.lab.child,'run',side_effect=slow),patch.object(self.lab.child,'_control',wraps=self.lab.child._control) as control:
            self.lab.advance();self.assertTrue(started.wait(1))
            self.parent.control('pause');self.lab.tick()
            self.assertTrue(any(call.args[0]=='stop' for call in control.call_args_list))
            release.set();self.lab.thread.join(3)
        self.assertEqual(self.lab.state['stage'],0)

    def test_student_view_starts_with_familiar_problem_not_data_command_list(self):
        text=content(self.lab.state)
        self.assertIn('Две чашки чая',text)
        for word in ['data verify','data eval','read_file','target','train:']:
            self.assertNotIn(word,text)
        self.lab.state['stage']=1
        self.assertIn('Обучения ещё не было',content(self.lab.state))
        self.assertIn('без запуска обучения',content(self.lab.state))

    def test_prepared_start_opens_only_fixed_practice_and_no_teacher_request(self):
        class Backend:
            def answer(self,*a):raise AssertionError('No model for prepared start')
            def close(self):pass
        runtime=TeacherRuntime(self.parent,Backend());self.parent.control('pause')
        runtime.start()
        try:
            deadline=time.monotonic()+2
            while self.parent.snapshot()['gate']!='open' and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(self.parent.snapshot()['gate'],'open')
        finally:runtime.close()

    def test_teacher_receives_current_button_and_stage_not_just_a_checklist(self):
        from coursekit.teacher import CodexTeacher
        self.lab.state['stage']=1;self.lab.save()
        self.parent._record('learning_lab_opened','student',{})
        teacher=CodexTeacher(self.parent)
        try:
            packet=json.loads(teacher.context([])[len(teacher.instruction)+1:])
            self.assertEqual(packet['learning_lab']['stage_name'],'Учебные пары')
            self.assertIn('подтвердить пары без обучения',packet['learning_lab']['next_action'])
            self.assertFalse(packet['learning_lab']['training_completed'])
        finally:teacher.close()

    def test_closed_popup_never_prompts_pressing_its_buttons_in_teacher_chat(self):
        from coursekit.learning_lab import teacher_view
        self.lab.state['stage']=6;self.lab.state['finished']=True;self.lab.save()
        self.parent._record('learning_lab_opened','student',{})
        self.parent._record('learning_lab_closed','student',{'stage':6,'finished':True})
        view=teacher_view(self.parent)
        self.assertFalse(view['visible'])
        self.assertIn('Сейчас F2',view['next_action'])
        self.assertNotIn('F5',view['next_action'])
        self.assertTrue(view['understanding_not_assessed_by_buttons'])

    def test_changed_after_prompt_is_rejected_before_showing_comparison(self):
        with patch.object(self.lab.child,'run',side_effect=self.fake_run):
            self.lab.advance();self.wait();self.lab.advance();self.lab.advance();self.wait()
        self.assertEqual(self.lab.state['stage'],3)
        def changed(*args,**kwargs):
            result=self.fake_run(*args,**kwargs);result['result']['prompt']='silently changed prompt';return result
        with patch.object(self.lab.child,'run',side_effect=changed):
            self.lab.advance();self.lab.thread.join(3)
        self.assertIn('парное сравнение не засчитано',self.lab.error)
        self.assertIsNone(self.lab.state['after']);self.assertEqual(self.lab.state['stage'],3)

    def test_pairs_do_not_mix_teaching_answers_with_old_model_output(self):
        self.lab.state['stage']=1
        self.lab.state['before']={'request':'Две чашки чая.','text':'UNIQUE_RAW_MODEL_OUTPUT','prompt':'actual input','settings':{}}
        main=content(self.lab.state)
        self.assertIn('Желаемый ответ:',main)
        self.assertNotIn('UNIQUE_RAW_MODEL_OUTPUT',main)
        self.assertIn('UNIQUE_RAW_MODEL_OUTPUT',content(self.lab.state,details=True))
