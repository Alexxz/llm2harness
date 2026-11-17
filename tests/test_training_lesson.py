import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from coursekit.classroom import Session, ClassroomError
from coursekit import training_lesson as lesson
from tools.make_training_data import MODEL_ID, MODEL_REVISION

ROOT=Path(__file__).resolve().parents[1]

class Trace:
    def __init__(self):self.events=[]
    def emit(self,name,**data):self.events.append((name,data))

class TrainingLessonTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        shutil.copytree(ROOT/'lessons/04-training-data',self.root/'lessons/04-training-data')
        self.session=Session.create('04',root=self.root)
        self.session.set_setting('max_tokens',128)

    def test_declared_endpoint_and_explicit_override(self):
        self.assertEqual(self.session.snapshot()['endpoint'],lesson.ENDPOINT)
        other=Session.create('04',root=self.root,endpoint='http://127.0.0.1:8099')
        self.assertEqual(other.snapshot()['endpoint'],'http://127.0.0.1:8099')
        self.assertIsNone(self.session.snapshot()['schema'])

    def test_data_inspection_and_leak_leave_original_data_unchanged(self):
        directory=self.root/'lessons/04-training-data/data'
        before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()}
        with patch.object(self.session,'run',side_effect=AssertionError('no inference')):
            self.assertIn('Правильный учебный ответ',lesson.data_command(self.session,['train','1']))
            evaluation=lesson.data_command(self.session,['eval','1'])
            self.assertNotIn('"arguments"',evaluation)
            self.assertIn('Совпадений',lesson.data_command(self.session,['verify']))
            self.assertIn('СИНТЕТИЧЕСКИЙ',lesson.data_command(self.session,['leak']))
        self.assertEqual(before,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()})

    def test_no_target_in_rendered_eval_inputs_and_hash_enforced(self):
        _,evaluation,_=lesson.bundle(self.session)
        for row in evaluation:
            self.assertNotIn(json.dumps(row['target'],ensure_ascii=False,separators=(',',':')),lesson.rendered(self.session,row))
        path=self.root/'lessons/04-training-data/data/inputs.json';path.write_text('{}')
        with self.assertRaisesRegex(ValueError,'изменились'):lesson.rendered(self.session,evaluation[0])

    def fake_baseline(self, fail_at=None):
        _,evaluation,_=lesson.bundle(self.session)
        outputs={lesson.rendered(self.session,row):json.dumps(row['target']) for row in evaluation}
        seen=[]
        class Client:
            def __init__(self,*a,**kw):pass
            def generate(self,prompt,**kwargs):
                seen.append((prompt,kwargs))
                if fail_at==len(seen):raise RuntimeError('synthetic HTTP failure')
                return {'text':outputs[prompt],'metadata':{'server':{'model':MODEL_ID+'@'+MODEL_REVISION,'tokens_predicted':20}}}
        profile=json.loads((self.root/'lessons/04-training-data/profile.json').read_text())
        request={'endpoint':lesson.ENDPOINT,'schema':None,'settings':self.session.snapshot()['settings']}
        trace=Trace()
        with patch.object(lesson,'profile',return_value=profile),patch.object(lesson,'ModelClient',Client):
            report=lesson.baseline(request,self.session,trace)
        return report,seen,trace

    def test_batch_fixed_six_counts_and_no_schema_or_target(self):
        report,seen,trace=self.fake_baseline()
        self.assertEqual(len(seen),6);self.assertEqual(len(trace.events),6)
        self.assertIsNone(report['adapter']);self.assertEqual(report['metrics']['semantic_correct'],{'count':6,'total':6,'rate':1.0})
        for _,kwargs in seen:
            self.assertNotIn('json_schema',kwargs)
            self.assertEqual(kwargs,{'max_tokens':128,'temperature':0,'seed':42})
        self.assertEqual(report['settings']['model'],MODEL_ID)

    def test_partial_batch_never_returns_a_complete_report(self):
        with self.assertRaisesRegex(RuntimeError,'synthetic'):self.fake_baseline(fail_at=3)

    def install_report(self,report,number=1):
        run_id=f'{number:032x}';directory=self.session.directory/'runs'/run_id;directory.mkdir()
        (directory/'result.json').write_text(json.dumps({'result':report,'status':'completed'}))
        self.session._record('run_completed','system',{'kind':'baseline','status':'completed','run_id':run_id})

    def test_freeze_is_idempotent_and_never_overwrites_another_baseline(self):
        report,_,_=self.fake_baseline();self.install_report(report)
        lesson.report_command(self.session,['freeze'])
        path=self.root/'work/training-before.json';before=path.read_bytes()
        lesson.report_command(self.session,['freeze']);self.assertEqual(path.read_bytes(),before)
        changed=copy.deepcopy(report);changed['seconds']+=1;self.install_report(changed,2)
        with self.assertRaisesRegex(ValueError,'не перезаписан'):lesson.report_command(self.session,['freeze'])
        self.assertEqual(path.read_bytes(),before)

    def test_freeze_rejects_symlink_and_new_series_needs_open_gate(self):
        report,_,_=self.fake_baseline();self.install_report(report)
        (self.root/'work').mkdir(exist_ok=True);outside=self.root/'unchanged';outside.write_text('keep')
        (self.root/'work/training-data').symlink_to(outside)
        with self.assertRaises(ValueError):lesson.report_command(self.session,['freeze'])
        self.assertEqual(outside.read_text(),'keep')
        with self.session.claim_owner():
            with self.assertRaises(ClassroomError):self.session.run('baseline')

    def test_stop_cancels_the_owned_batch_before_following_cases(self):
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        entered=threading.Event();release=threading.Event();calls=[]
        p=json.loads((self.root/'lessons/04-training-data/profile.json').read_text())
        p['service']='course-training-model-v1'
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def send(self,data):
                body=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers()
                try:self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError):pass
            def do_GET(self):self.send(p)
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(data);entered.set();release.wait(8)
                self.send({'content':'{}','model':MODEL_ID+'@'+MODEL_REVISION})
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.daemon_threads=True
        serving=threading.Thread(target=server.serve_forever,daemon=True);serving.start()
        session=Session.create('04',root=self.root,endpoint=f'http://127.0.0.1:{server.server_port}')
        session.set_setting('max_tokens',128)
        try:
            with session.claim_owner():
                session.control('resume');results=[]
                running=threading.Thread(target=lambda:results.append(session.run('baseline')))
                running.start()
                try:
                    self.assertTrue(entered.wait(8),'Batch did not reach its first model request')
                    session.control('stop');running.join(12)
                    self.assertFalse(running.is_alive())
                    self.assertEqual(results[0]['status'],'cancelled')
                    self.assertEqual(results[0]['cleanup'],'confirmed')
                    self.assertEqual(len(calls),1)
                    with self.assertRaisesRegex(ValueError,'Законченной'):lesson.last_report(session)
                finally:
                    release.set();running.join(12)
        finally:
            release.set();server.shutdown();server.server_close();serving.join()

    def test_batch_respects_explicit_request_budget(self):
        self.session.set_setting('max_requests',1)
        with self.assertRaisesRegex(ValueError,'max_requests'):self.fake_baseline()
