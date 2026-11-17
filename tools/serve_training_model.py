#!/usr/bin/env python3
"""Loopback-only, offline Transformers inference for the fixed lessons 04–05 profile."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.make_training_data import MODEL_ID, MODEL_REVISION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8089)
    parser.add_argument('--cache-dir', type=Path, default=Path(__file__).resolve().parents[1]/'work/model-cache')
    args = parser.parse_args()
    os.environ.setdefault('HF_HOME', str(Path(__file__).resolve().parents[1]/'work/hf-home'))
    os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN']='1'
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, set_seed
    torch.set_num_threads(4)
    options = dict(revision=MODEL_REVISION, cache_dir=str(args.cache_dir), local_files_only=True, token=False, trust_remote_code=False)
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, **options)
    model = AutoModelForCausalLM.from_pretrained(MODEL_ID, torch_dtype=torch.float32, use_safetensors=True, **options).to('cpu').eval()
    profile = {'service':'course-training-model-v1','model':MODEL_ID,'revision':MODEL_REVISION,'dtype':'float32','device':'cpu','threads':4,'adapter':None,'chat_template_sha256':hashlib.sha256((tokenizer.chat_template or '').encode()).hexdigest(),'versions':{name:importlib.metadata.version(name) for name in ['torch','transformers','peft']}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def setup(self):
            super().setup()
            self.connection.settimeout(30)
        def reply(self, data, status=200):
            body=json.dumps(data,ensure_ascii=False).encode()
            self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_GET(self):
            if self.path in {'/health','/props'}:
                self.reply(profile)
            else:
                self.reply({'error':'Unknown path'},400)
        def do_POST(self):
            if self.path != '/completion':
                self.reply({'error':'Unknown path'},400);return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=262144:raise ValueError('Request size limit')
                data=json.loads(self.rfile.read(size))
                prompt=data.get('prompt');limit=data.get('n_predict',128);temperature=data.get('temperature',0);seed=data.get('seed',42)
                if not isinstance(prompt,str) or not prompt or type(limit) is not int or not 1<=limit<=512 or type(seed) is not int or not 0<=seed<2**32 or type(temperature) not in (int,float) or not 0<=temperature<=2 or data.get('json_schema') is not None:
                    raise ValueError('Profile needs text, max_tokens 1..512, temperature 0..2, seed, no generation schema')
                inputs=tokenizer(prompt,return_tensors='pt',add_special_tokens=False)
                if inputs['input_ids'].shape[-1]+limit>4096:raise ValueError('Context limit 4096')
                set_seed(seed);started=time.monotonic()
                options={'max_new_tokens':limit,'do_sample':temperature>0,'pad_token_id':tokenizer.eos_token_id}
                if temperature>0:options['temperature']=temperature
                with torch.inference_mode():
                    generated=model.generate(**inputs,**options)
                tail=generated[0,inputs['input_ids'].shape[-1]:]
                self.reply({'content':tokenizer.decode(tail,skip_special_tokens=True),'model':MODEL_ID+'@'+MODEL_REVISION,'tokens_evaluated':inputs['input_ids'].shape[-1],'tokens_predicted':len(tail),'timings':{'elapsed_seconds':round(time.monotonic()-started,3)},'stopped_limit':len(tail)>=limit,'profile':profile})
            except (ValueError, TypeError, KeyError) as error:
                self.reply({'error':str(error)},400)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self.reply({'error':'Local inference failed'},500)
    print(json.dumps({'ready':True,**profile,'endpoint':f'http://127.0.0.1:{args.port}'},ensure_ascii=False),flush=True)
    HTTPServer(('127.0.0.1',args.port),Handler).serve_forever()


if __name__=='__main__':
    main()
