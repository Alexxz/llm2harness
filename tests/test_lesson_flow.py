"""The question must follow the explanation and newly observed experience."""
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from coursekit.classroom import Session
from coursekit.lesson_flow import progress
from coursekit.teacher import TeacherRuntime
from coursekit.workbench import Workspace

ROOT=Path(__file__).resolve().parents[1]


class LessonFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root=Path(self.temporary.name)
        shutil.copytree(ROOT/'lessons/00-interface',root/'lessons/00-interface')
        self.session=Session.create('00',root=root)
        self.lab=Workspace(self.session)

    def intro(self):
        return self.session._record('teacher_turn_done','teacher',{'through':0,'source':'workspace_explanation','phase_index':0,'reply':self.lab.phase['explanation']+'\n\n'+self.lab.phase['action'],'control':'none'})

    def test_actions_before_instruction_do_not_count(self):
        self.lab.save('focused');self.lab.save('teacher_focus')
        self.intro()
        self.assertFalse(progress(self.session)['experience_completed'])
        self.lab.save('teacher_focus');self.lab.save('focused')
        self.assertFalse(progress(self.session)['experience_completed'])
        self.lab.save('teacher_focus')
        self.assertTrue(progress(self.session)['experience_completed'])
        self.assertFalse(progress(self.session)['ready_to_assess'])

    def test_all_lessons_have_explicit_teaching_cycle(self):
        for directory in (ROOT/'lessons').iterdir():
            if directory.name[:2] not in {'00','01','02','03','04'}:continue
            spec=json.loads((directory/'workspace.json').read_text())
            for phase in spec['phases']:
                for key in ('explanation','action','observe','question','evidence'):
                    self.assertTrue(phase[key],(directory.name,phase['name'],key))

    def test_question_is_emitted_after_new_actions_not_on_first_reply(self):
        calls=[]
        class Backend:
            def answer(self,events):
                calls.append(events)
                return {'reply':'Сейчас ответим на вопрос.','note':'help','control':'advance'}
            def close(self):pass
        runtime=TeacherRuntime(self.session,Backend())
        def wait(predicate):
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                if predicate():return
                time.sleep(.02)
            self.fail('Expected teaching stage did not appear')
        runtime.start()
        try:
            wait(lambda:progress(self.session)['introduced'])
            turns=[e for e in self.session.events(limit=100) if e['event']=='teacher_turn_done']
            self.assertNotIn(self.lab.phase['question'],turns[0]['data']['reply'])
            self.assertIn(self.lab.phase['explanation'],turns[0]['data']['reply'])
            self.assertFalse(progress(self.session)['question_asked'])
            self.session.ask('Что значит поле Вход модели?')
            wait(lambda:bool(calls))
            time.sleep(.1)
            self.assertEqual(progress(self.session)['phase_index'],0)
            self.assertFalse(progress(self.session)['question_asked'])
            self.lab.save('focused');self.lab.save('teacher_focus')
            wait(lambda:progress(self.session)['question_asked'])
            self.assertFalse(progress(self.session)['student_replied'])
        finally:runtime.close()
        self.assertEqual(self.session.snapshot()['gate'],'paused')

    def test_comparison_contains_old_answer_even_after_a_preset_cleared_the_screen(self):
        from coursekit.lesson_flow import render_observation
        events=[]
        def completed(sequence,preset,prompt,answer):
            return {'sequence':sequence,'event':'workspace_state','data':{'action':'completed','state':{'preset':preset,'mode':'request','model':'original','parameters':{'max_tokens':32,'temperature':0,'seed':42},'last_input':{'prompt':prompt,'model':'original','parameters':{'max_tokens':32,'temperature':0,'seed':42}},'result':{'text':answer},'prompt':prompt,'output':answer,'error':''}}}
        events.append(completed(1,'baseline','old input','old actual answer'))
        events.append({'sequence':2,'event':'teacher_turn_done','data':{'source':'workspace_explanation','phase_index':1}})
        events.append({'sequence':3,'event':'workspace_state','data':{'action':'preset_loaded','state':{'output':'','result':None}}})
        events.append(completed(4,'variation','new input','new actual answer'))
        flow={'phase_index':1,'phase':{'show_runs':True,'observe':'Сравним сохранённые ответы.','reference_runs':[{'preset':'baseline'}]}}
        text=render_observation(flow,events)
        for value in ('old input','new input','old actual answer','new actual answer','max_tokens=32'):
            self.assertIn(value,text)

    def test_intro_select_and_run_are_one_ordered_experience(self):
        from coursekit.workbench import advance_phase
        advance_phase(self.session);advance_phase(self.session)
        phase=self.lab.phase
        self.assertTrue(phase['requests'])
        self.session._record('teacher_turn_done','teacher',{'source':'workspace_explanation','phase_index':2,'through':0,'reply':phase['explanation'],'control':'none'})
        self.lab.state.update(result={'text':'too early'},error='')
        self.lab.save('completed')
        self.lab.load('colors');self.lab.save('teacher_focus')
        self.assertFalse(progress(self.session)['experience_completed'])
        self.lab.state.update(result={'text':'actual answer'},output='actual answer',error='')
        self.lab.save('completed')
        self.assertTrue(progress(self.session)['experience_completed'])
