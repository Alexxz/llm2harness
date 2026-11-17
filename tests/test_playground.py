from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from coursekit.classroom import Session
from coursekit.playground import Playground

class Server(BaseHTTPRequestHandler):
    requests = []
    release = threading.Event()
    def log_message(self,*args):pass
    def do_POST(self):
        type(self).requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        type(self).release.wait(15)
        try:
            self.send_response(200);self.end_headers()
            self.wfile.write(b'{"content":"synthetic playground result","tokens_predicted":3}')
        except (BrokenPipeError,ConnectionResetError):pass

class PlaygroundTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);lesson=root/'lessons/01-test';(lesson/'prompts').mkdir(parents=True)
        (lesson/'lesson.json').write_text(json.dumps({'id':'01','experiments':[]}))
        (lesson/'prompts/baseline.txt').write_text('main lesson prompt')
        Server.requests=[];Server.release=threading.Event();Server.release.set()
        server=ThreadingHTTPServer(('127.0.0.1',0),Server);server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join,3);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        self.parent=Session.create('01',root=root,endpoint=f'http://127.0.0.1:{server.server_port}')
        self.playground=Playground(self.parent);self.addCleanup(self.playground.close)
        # A held request must outlive the five-second cancellation observation.
        self.addCleanup(Server.release.set)
    def wait(self,predicate):
        deadline=time.monotonic()+5
        while not predicate():
            if time.monotonic()>deadline:self.fail('Timed out')
            time.sleep(.02)
    def test_request_parameters_evidence_persistence_and_lesson_isolation(self):
        before=self.parent.snapshot()
        self.playground.start('sandbox prompt',{'max_tokens':'12','temperature':'0.7','seed':'23'})
        self.wait(lambda:not self.playground.busy)
        self.assertEqual(Server.requests[0]['prompt'],'sandbox prompt')
        self.assertEqual(Server.requests[0]['n_predict'],12)
        self.assertEqual(Server.requests[0]['temperature'],.7)
        self.assertEqual(Server.requests[0]['seed'],23)
        after=self.parent.snapshot()
        for key in ['prompt','settings','gate','last_run','active_run','capabilities']:
            self.assertEqual(before[key],after[key])
        events=self.parent.events(limit=100)
        self.assertTrue(any(e['event']=='playground_result' and e['data']['result']['text']=='synthetic playground result' for e in events))
        self.assertFalse(any(e['event']=='model_response' for e in events))
        self.playground.close()
        self.assertIsNone(self.playground.child.snapshot()['owner_pid'])
        reopened=Playground(self.parent)
        self.assertEqual(reopened.draft['prompt'],'sandbox prompt')
        self.assertEqual(reopened.draft['output'],'synthetic playground result')
        reopened.close()
    def test_teacher_stop_blocks_new_calls_and_cancels_owned_request(self):
        Server.release.clear()
        self.playground.start('slow request',{'max_tokens':12,'temperature':0,'seed':42})
        self.wait(lambda:bool(Server.requests))
        self.parent.control('pause');self.playground.tick()
        self.wait(lambda:not self.playground.busy)
        self.assertEqual(self.playground.result['status'],'cancelled')
        with self.assertRaises(ValueError):
            self.playground.start('blocked',{'max_tokens':12,'temperature':0,'seed':42})
        Server.release.set()
    def test_invalid_input_and_missing_capability_do_not_send_requests(self):
        for prompt,settings in [(' ',{'max_tokens':12,'temperature':0,'seed':42}),('x',{'max_tokens':99999,'temperature':0,'seed':42}),('x',{'max_tokens':12,'temperature':'nan','seed':42})]:
            with self.assertRaises(ValueError):self.playground.start(prompt,settings)
        self.parent.control('permit',capabilities=['doctor'])
        with self.assertRaises(ValueError):self.playground.start('x',{'max_tokens':12,'temperature':0,'seed':42})
        self.assertEqual(Server.requests,[])

    def test_multiple_edited_requests_reuse_window_without_changing_lesson(self):
        before=self.parent.snapshot()
        child_id=None
        for index,(prompt,tokens,temp,seed) in enumerate([('first prompt',8,'0,6',7),('edited prompt',16,'0.2',42),('third prompt',24,'0',23)]):
            self.playground.start(prompt,{'max_tokens':str(tokens),'temperature':temp,'seed':str(seed)})
            self.wait(lambda:not self.playground.busy)
            self.assertEqual(self.playground.result['status'],'completed')
            if child_id is None:child_id=self.playground.child.id
            self.assertEqual(self.playground.child.id,child_id)
            self.assertEqual(Server.requests[index]['prompt'],prompt)
            self.assertEqual(Server.requests[index]['n_predict'],tokens)
            self.assertEqual(Server.requests[index]['temperature'],float(temp.replace(',','.')))
            self.assertEqual(Server.requests[index]['seed'],seed)
        after=self.parent.snapshot()
        self.assertEqual(before['prompt'],after['prompt'])
        self.assertEqual(before['settings'],after['settings'])
        self.assertEqual(len([e for e in self.parent.events(limit=100) if e['event']=='playground_result']),3)
