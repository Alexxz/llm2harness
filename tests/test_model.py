"""Проверки HTTP границы стенда с синтетическим локальным сервером, без LLM."""

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from coursekit.model import ModelClient, ModelError
from coursekit.trace import Trace


class Handler(BaseHTTPRequestHandler):
    mode = "reply"
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append({"path": self.path, "payload": payload})
        if self.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://192.0.2.1/never-follow")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if self.mode == "slow":
            for _ in range(50):
                try:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                time.sleep(0.03)
            return
        if self.mode == "invalid":
            self.wfile.write(b"not JSON")
            return
        if self.mode == "oversize":
            try:
                self.wfile.write(b" " * (2 * 1024 * 1024 + 1))
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self.wfile.write(json.dumps({"content": "synthetic response", "tokens_predicted": 2}).encode())


class ModelBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        Handler.mode = "reply"
        Handler.requests = []

    def test_one_request_preserves_prompt_settings_and_evidence(self):
        trace = Trace()
        result = ModelClient(self.endpoint, trace=trace).generate("точный текст", max_tokens=17, temperature=0.2, seed=7)
        self.assertEqual(result["text"], "synthetic response")
        self.assertEqual(len(Handler.requests), 1)
        request = Handler.requests[0]
        self.assertEqual(request["path"], "/completion")
        self.assertEqual(request["payload"]["prompt"], "точный текст")
        self.assertEqual(request["payload"]["n_predict"], 17)
        self.assertEqual(request["payload"]["seed"], 7)
        self.assertEqual([event["event"] for event in trace.events], ["model_request", "model_response"])

    def test_schema_is_forwarded_without_changing_prompt(self):
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        ModelClient(self.endpoint).generate("input", json_schema=schema)
        self.assertEqual(Handler.requests[0]["payload"]["json_schema"], schema)
        self.assertEqual(Handler.requests[0]["payload"]["prompt"], "input")

    def test_remote_and_embedded_credentials_are_rejected_before_request(self):
        for endpoint in ("http://192.0.2.1:8080", "http://user:placeholder@127.0.0.1:8080", "http://127.0.0.1:8080/path"):
            with self.assertRaises(ValueError):
                ModelClient(endpoint)
        self.assertEqual(Handler.requests, [])

    def test_redirect_is_not_followed(self):
        Handler.mode = "redirect"
        with self.assertRaises(ModelError):
            ModelClient(self.endpoint).generate("input")
        self.assertEqual(len(Handler.requests), 1)

    def test_invalid_or_unbounded_responses_fail_without_retry(self):
        for mode in ("invalid", "oversize"):
            Handler.mode = mode
            Handler.requests = []
            with self.assertRaises(ModelError):
                ModelClient(self.endpoint).generate("input")
            self.assertEqual(len(Handler.requests), 1)

    def test_total_timeout_stops_a_trickling_response(self):
        Handler.mode = "slow"
        started = time.monotonic()
        with self.assertRaisesRegex(ModelError, "Общий таймаут"):
            ModelClient(self.endpoint, timeout=0.3).generate("input")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(len(Handler.requests), 1)


if __name__ == "__main__":
    unittest.main()
