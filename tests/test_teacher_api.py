import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from coursekit.classroom import Session
from coursekit.teacher import ResponsesTeacher, CommandTeacher, CodexTeacher, teacher_backend, teacher_codex_argv

class TeacherAPITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); lesson = self.root/'lessons/01-test'
        (lesson/'prompts').mkdir(parents=True)
        (lesson/'lesson.json').write_text(json.dumps({'id':'01','experiments':[]}))
        (lesson/'prompts/baseline.txt').write_text('test input')
        self.session = Session.create('01', root=self.root)
        self.seen=[]; self.status=200
        self.reply={'status':'completed','model':'fixture-model','usage':{'input_tokens':12},'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'reply':'Учебный ответ','note':'Объяснено','control':'none'})}]}]}
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                owner.authorization=self.headers.get('Authorization')
                self.send_response(owner.status);self.end_headers()
                self.wfile.write(json.dumps(owner.reply).encode())
            def log_message(self,*args): pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop_server)
        environment=patch.dict(os.environ,{'COURSE_TEACHER_BACKEND':'api','COURSE_TEACHER_API_MODEL':'fixture-model','OPENAI_API_KEY':'synthetic-test-value','COURSE_TEACHER_API_ENDPOINT':f'http://127.0.0.1:{self.server.server_port}/v1/responses'},clear=True)
        environment.start();self.addCleanup(environment.stop)
        self.client=ResponsesTeacher(self.session);self.addCleanup(self.client.close)
    def stop_server(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
    def test_subprocess_sends_text_only_and_records_usage_without_credentials(self):
        result=self.client.answer([]);request=self.seen[0]
        self.assertEqual(result['reply'],'Учебный ответ')
        self.assertEqual(request['tools'],[]);self.assertEqual(request['tool_choice'],'none')
        self.assertFalse(request['store']);self.assertTrue(request['text']['format']['strict'])
        self.assertIn('test input',request['input'])
        self.assertEqual(self.authorization,'Bearer synthetic-test-value')
        events=self.session.events(limit=100)
        usage=next(e for e in events if e['event']=='teacher_api_usage')
        self.assertEqual(usage['data']['usage']['input_tokens'],12)
        self.assertNotIn('synthetic-test-value',json.dumps(events))
        self.assertNotIn('synthetic-test-value',json.dumps(request))
    def test_provider_error_is_redacted_and_not_retried(self):
        self.status=401;self.reply={'error':'private marker'}
        with self.assertRaises(RuntimeError) as raised:self.client.answer([])
        self.assertNotIn('private marker',str(raised.exception));self.assertEqual(len(self.seen),1)
    def test_redirect_is_rejected(self):
        self.status=302
        with self.assertRaises(RuntimeError):self.client.answer([])
        self.assertEqual(len(self.seen),1)
    def test_missing_key_makes_no_request(self):
        os.environ.pop('OPENAI_API_KEY')
        with self.assertRaises(RuntimeError):self.client.answer([])
        self.assertEqual(self.seen,[])
    def test_remote_http_is_rejected_before_sending_key(self):
        os.environ['COURSE_TEACHER_API_ENDPOINT']='http://provider.example/v1/responses'
        with self.assertRaises(RuntimeError):self.client.answer([])
        self.assertEqual(self.seen,[])
    def test_bad_json_container_and_tool_call_are_rejected(self):
        self.reply['output'][0]['content'][0]['text']='[]'
        with self.assertRaises(ValueError):self.client.answer([])
        self.reply['output']=[{'type':'function_call','name':'unexpected'}]
        with self.assertRaises(RuntimeError):self.client.answer([])
    def test_default_is_codex_and_invocation_is_bounded(self):
        with patch.dict(os.environ,{},clear=True):
            backend=teacher_backend(self.session);self.addCleanup(backend.close)
            self.assertIsInstance(backend,CodexTeacher)
            argv=teacher_codex_argv('codex','/tmp/example','schema.json','out.json')
        for value in ['--ignore-user-config','--ignore-rules','--ephemeral','read-only','mcp_servers={}','features.shell_tool=false','approval_policy="never"']:
            self.assertIn(value,argv)
    def test_generic_adapter_receives_packet_and_schema(self):
        adapter=self.root/'adapter.py'
        adapter.write_text("import json,sys\nr=json.load(sys.stdin)\nassert 'test input' in r['input']\nassert r['timeout']>0 and 'reply' in r['schema']['properties']\nprint(json.dumps({'reply':'adapter reply','note':'fixture','control':'pause'}))\n")
        with patch.dict(os.environ,{'COURSE_TEACHER_COMMAND':f'/usr/bin/python3 {adapter}'}):
            backend=CommandTeacher(self.session);self.addCleanup(backend.close)
            self.assertEqual(backend.answer([])['control'],'pause')
    def test_generic_adapter_diagnostics_are_private(self):
        adapter=self.root/'bad-adapter.py';adapter.write_text("import sys\nprint('private marker')\nprint('private marker',file=sys.stderr)\n")
        with patch.dict(os.environ,{'COURSE_TEACHER_COMMAND':f'/usr/bin/python3 {adapter}'}):
            backend=CommandTeacher(self.session);self.addCleanup(backend.close)
            with self.assertRaises(RuntimeError) as raised:backend.answer([])
            self.assertNotIn('private marker',str(raised.exception))
