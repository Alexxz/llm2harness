import curses
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from coursekit.classroom import Session
from coursekit.prompt_editor import history_text, _screen

class PromptEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        lesson = root/'lessons/02-test'; (lesson/'prompts').mkdir(parents=True)
        (lesson/'lesson.json').write_text(json.dumps({'id':'02','experiments':[]}))
        for name, text in [('baseline.txt','initial'),('first-turn.txt','Начало'),('variation.txt','Вопрос?\nОтвет:\n')]:
            (lesson/'prompts'/name).write_text(text)
        self.session = Session.create('02',root=root)
        self.run_id = 'a'*32
        self.session._record('model_request','model',{'run_id':self.run_id,'request':{'prompt':'Начало'}})
        run = self.session.directory/'runs'/self.run_id; run.mkdir(parents=True)
        (run/'result.json').write_text(json.dumps({'status':'completed','result':{'text':' полный\nответ\n'}}))

    def test_exact_actual_response_and_question_are_preserved(self):
        text, source = history_text(self.session)
        self.assertEqual(text,'Начало полный\nответ\n\n\nВопрос?\nОтвет:\n')
        self.assertEqual(source,self.run_id)
        self.assertEqual(self.session.snapshot()['prompt'],'initial')

    def screen(self, keys):
        class Screen:
            def timeout(self,*a): pass
            def getmaxyx(self): return 30,100
            def erase(self): pass
            def addnstr(self,*a): pass
            def move(self,*a): pass
            def get_wch(self): return next(keys)
        return Screen()

    def test_assemble_review_and_save_without_model_call(self):
        with patch.object(self.session,'run',side_effect=AssertionError('no model')):
            _screen(self.screen(iter([curses.KEY_F6,curses.KEY_F5])),self.session)
        self.assertEqual(self.session.snapshot()['prompt'],history_text(self.session)[0])

    def test_escape_discards_edits_and_assembly(self):
        _screen(self.screen(iter([curses.KEY_F6,'x','\x1b'])),self.session)
        self.assertEqual(self.session.snapshot()['prompt'],'initial')
