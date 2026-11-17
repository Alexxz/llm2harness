#!/usr/bin/env python3
"""Measure short lesson-00 candidates against existing servers; never change them."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from coursekit.preparation import props

QUESTIONS=[
 {'id':'earth','question':'Как называется планета, на которой мы живём?','expected':r'земл'},
 {'id':'snow','question':'Какого цвета свежий снег?','expected':r'бел'},
 {'id':'cat','question':'Верно ли утверждение: у кошки четыре лапы?','expected':r'да|верно|четыре'},
 {'id':'sum','question':'Сколько будет 2 + 2?','expected':r'4|четыре'},
]

SELECTED = [
 {'id':'baseline','title':'Вопрос о планете','prompt':'Вопрос: Как называется наша планета?\nОтвет одним словом:','max_tokens':4,'expected':' Земля.'},
 {'id':'colors','title':'Цвет снега','prompt':'Вопрос: Снег белый или чёрный?\nОтвет:','max_tokens':5,'expected':' Снег белый'},
 {'id':'animals','title':'Лапы кошки','prompt':'Вопрос: Сколько лап у кошки?\nОтвет:','max_tokens':9,'expected':' Кошки имеют 4 лапы.'},
 {'id':'counting','title':'Простой счёт','prompt':'Вопрос: Сколько будет 2 + 2?\nОтвет:','max_tokens':2,'expected':' 4'},
]

def request(endpoint,prompt,temperature,seed,limit):
 payload={'prompt':prompt,'n_predict':limit,'temperature':temperature,'seed':seed,'stream':False,'cache_prompt':False}
 result=subprocess.run([sys.executable,'-I',str(ROOT/'coursekit/model_http_worker.py')],input=json.dumps({'endpoint':endpoint,'timeout':60,'payload':payload},ensure_ascii=False),text=True,capture_output=True,timeout=65)
 if result.returncode:raise RuntimeError(result.stderr.strip())
 return payload,json.loads(result.stdout)

def assess(q,result):
 text=result['content'];words=re.findall(r'\w+',text)
 stop=result.get('stop_type')
 natural=(stop=='eos') or (result.get('stopped_eos') is True) or (result.get('stopped_limit') is False and result['tokens_predicted']<64)
 relevant=bool(re.search(q['expected'],text,re.I))
 # Exact automated checks are conservative screening aids; final output needs human review.
 no_new_question='?' not in text and not re.search(r'Вопрос\s*:|Question\s*:|\n\s*[-*\d]+[.)]',text,re.I)
 return {'words':len(words),'short':0<len(words)<=20,'natural_stop':natural,'expected_topic':relevant,'no_new_question_or_list':bool(no_new_question),'candidate_pass':0<len(words)<=20 and natural and relevant and bool(no_new_question)}

def prompt(q,kind):
 if kind=='plain':return 'Ответь коротко, одним предложением.\nВопрос: '+q['question']+'\nОтвет:'
 if kind=='chat':return '<|im_start|>user\n'+q['question']+' Ответь коротко, одним предложением.<|im_end|>\n<|im_start|>assistant\n'
 if kind=='short':return 'Вопрос: '+q['question']+'\nКраткий ответ:'
 raise ValueError(kind)

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--profile',choices=['base','instruct'],required=True);parser.add_argument('--output',type=Path);parser.add_argument('--stage',choices=['screening','selected'],default='screening');args=parser.parse_args()
 endpoint='http://127.0.0.1:8088' if args.profile=='base' else 'http://127.0.0.1:8089'
 out=args.output or ROOT/'results/preparation'/('intro-calibration-'+args.profile+'-'+uuid.uuid4().hex);out.mkdir(parents=True,exist_ok=False)
 profile=props(endpoint);(out/'props.json').write_text(json.dumps(profile,ensure_ascii=False,indent=2)+'\n')
 if args.profile=='base':
  expected=json.loads((ROOT/'resources/models.json').read_text())['base']['local_name']
  actual=profile.get('model_path',profile.get('default_generation_settings',{}).get('model',''))
  if Path(actual).name!=expected:raise ValueError('Existing endpoint is not the pinned base profile; no substitution performed')
 else:
  from tools.make_training_data import MODEL_ID,MODEL_REVISION
  if profile.get('model')!=MODEL_ID or profile.get('revision')!=MODEL_REVISION:raise ValueError('Existing endpoint is not the pinned instruction profile; no substitution performed')
 print(out,flush=True)
 report={'purpose':'author-preset-calibration','profile':args.profile,'endpoint':endpoint,'protocol':{'max_words':20,'max_tokens':64,'no_postprocessing':True,'no_extra_stop_or_grammar':True},'runs':[]}
 def run(q,kind,temperature,seed,stage):
  text=prompt(q,kind);payload,result=request(endpoint,text,temperature,seed,64)
  row={'question_id':q['id'],'kind':kind,'stage':stage,'request':payload,'raw_response':result,'assessment':assess(q,result)}
  report['runs'].append(row);(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
  print(q['id'],kind,temperature,seed,repr(result['content']),result.get('stop_type',result.get('stopped_limit')),row['assessment']['candidate_pass'],flush=True)
 if args.stage=='selected':
  if args.profile!='base':raise ValueError('Selected presets are calibrated only for base; no implicit model substitution')
  report['protocol'].pop('max_tokens',None)
  report['protocol']['max_tokens_by_preset']={c['id']:c['max_tokens'] for c in SELECTED}
  report['protocol']['temperature']=.2
  report['protocol']['termination']='budget_limit_allowed_by_user; raw response must be a whole correct answer'
  report['selected']=SELECTED
  for candidate in SELECTED:
   for trial,seed in enumerate([42]*5+[0,1,7,43,44],1):
    payload,result=request(endpoint,candidate['prompt'],.2,seed,candidate['max_tokens'])
    report['runs'].append({'preset':candidate['id'],'trial':trial,'stage':'exact_repeat' if trial<=5 else 'seed_variation','request':payload,'raw_response':result,'identical_to_selected':result['content']==candidate['expected']})
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(candidate['id'],trial,seed,repr(result['content']),result.get('stop_type'),flush=True)
 elif args.profile=='base':
  for q in QUESTIONS:
   for kind,temp in [('short',.3),('plain',.7),('plain',.3)]:run(q,kind,temp,42,'screening')
 else:
  for q in QUESTIONS:
   for kind in ('plain','chat'):run(q,kind,0,42,'screening')
 if args.stage=='selected':
  report['summary']={c['id']:{stage:{'matching':sum(x['identical_to_selected'] for x in report['runs'] if x['preset']==c['id'] and x['stage']==stage),'total':sum(1 for x in report['runs'] if x['preset']==c['id'] and x['stage']==stage)} for stage in ('exact_repeat','seed_variation')} for c in SELECTED}
 report['complete']=True
 (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
 print(out/'report.json',flush=True)
 if args.stage=='selected':
  return 0 if all(x['identical_to_selected'] for x in report['runs'] if x['stage']=='exact_repeat') else 1
 return 0
if __name__=='__main__':raise SystemExit(main())
