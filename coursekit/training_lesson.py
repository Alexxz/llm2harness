"""Lesson 04: visible data, independent evaluation and a frozen before-report."""
import copy
import hashlib
import re
import json
import time
import urllib.request
from pathlib import Path
import subprocess
import sys

from .classroom import MAX_TEXT, MAX_RESULT_BYTES, _read, _atomic, _mkdir, _plain_path
from .model import ModelClient
from tools.make_training_data import load_bundle, validate_rows, score_text, MODEL_ID, MODEL_REVISION

ENDPOINT = 'http://127.0.0.1:8089'


def bundle(session):
    if session.snapshot()['lesson'] != '04':
        raise ValueError('Эта команда подготовлена для урока 04.')
    directory,_=session._find_lesson('04')
    # Validate protected paths before the existing dataset checker opens files.
    for name in ('train.jsonl','eval.jsonl','command.schema.json','manifest.json'):
        _read(directory/'data'/name,session.root,MAX_RESULT_BYTES)
    return load_bundle(directory/'data')


def profile(endpoint=ENDPOINT):
    ModelClient(endpoint)  # Loopback only, no credentials or redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*a,**kw):raise ValueError('Redirect disabled')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with opener.open(endpoint.rstrip('/')+'/props',timeout=3) as response:
        value=json.loads(response.read(65536))
    if value.get('service')!='course-training-model-v1' or value.get('model')!=MODEL_ID or value.get('revision')!=MODEL_REVISION or value.get('adapter') is not None or value.get('dtype')!='float32' or value.get('device')!='cpu' or value.get('threads')!=4:
        raise ValueError('Нужен подготовленный локальный профиль Qwen 0.5B без адаптера; другой сервер не подставляется.')
    return value


def ensure_server(root):
    try:
        profile();return
    except OSError:
        pass
    root=Path(root);python=root/'work/training-venv/bin/python'
    if not python.is_file():raise ValueError('Локальный профиль обучения не подготовлен. См. SETUP.md урока 04.')
    print('Запускается подготовленная локальная Qwen 0.5B на CPU. Загрузок из сети нет…',flush=True)
    process=subprocess.Popen([str(python),'-I',str(root/'tools/serve_training_model.py')],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        try:
            profile();return
        except OSError:
            if process.poll() is not None:break
            time.sleep(.1)
    if process.poll() is None:
        process.terminate()
        try:process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill();process.wait()
    raise ValueError('Не удалось запустить локальный профиль. См. SETUP.md урока 04; другие веса не подставлялись.')


def rendered(session, row):
    directory,_=session._find_lesson('04')
    raw=_read(directory/'data/inputs.json',session.root,MAX_RESULT_BYTES)
    expected=json.loads(_read(directory/'profile.json',session.root,MAX_TEXT))
    if hashlib.sha256(raw).hexdigest()!=expected['inputs_sha256']:
        raise ValueError('Подготовленные входы изменились; исходная серия прекращена.')
    inputs=json.loads(raw)
    return inputs[row['id']]


def data_command(session,args):
    train,evaluation,manifest=bundle(session)
    if not args:
        result=f'Обучение train: {len(train)} примеров с правильными ответами. Проверка eval: {len(evaluation)} отдельных задач. Все примеры придуманы генератором, это не ответы модели.\ndata train 1 — разобрать учебный пример; data eval 1 — посмотреть проверочный вход. Модель не вызывается.'
    elif args==['verify']:
        result='Хеши неизменны. Совпадений сущностей, формулировок, входов и готовых ответов между train и eval нет. Это проверка нашего набора, а не доказательство независимости от всех данных предобучения.\nДАЛЬШЕ: data leak покажет, что произойдёт при попадании учебной записи в проверку.'
    elif args==['leak']:
        contaminated=copy.deepcopy(evaluation);contaminated[0]['entity_id']=train[0]['entity_id']
        try:validate_rows(train,contaminated)
        except ValueError:
            result='СИНТЕТИЧЕСКИЙ ОПЫТ: в копию eval подставили сущность из train. Проверка отклонила пересечение. Исходные данные не менялись. Проверять на знакомых учебных примерах — не то же, что на новых.\nДАЛЬШЕ: вернись в F2 обсудить, зачем разделение; затем преподаватель разрешит baseline — серию из 6 настоящих запросов.'
        else:raise ValueError('Измеритель не обнаружил специально внесённое пересечение')
    elif len(args)==2 and args[0] in {'train','eval'} and args[1].isdigit():
        rows=train if args[0]=='train' else evaluation;index=int(args[1])-1
        if not 0<=index<len(rows):raise ValueError('Номер примера вне набора')
        row=rows[index];result=f"{args[0]} №{index+1}, {row['id']}\nВходная просьба: {row['messages'][1]['content']}\n"
        if args[0]=='train':
            result+='Правильный учебный ответ: '+json.dumps(row['target'],ensure_ascii=False)+'\nОн станет ответом для обучения, а не скрытой частью проверочного запроса.\nДАЛЬШЕ: data eval 1 — посмотреть отдельный проверочный вход.'
        else:
            result+='Эталон не добавляется в запрос модели. Его сравним с ответом после генерации.\nДАЛЬШЕ: data verify — проверить разбиение и неизменность данных.'
    else:raise ValueError('data | data train 1 | data eval 1 | data verify | data leak')
    session._record('training_data_inspected','student',{'args':args,'result':result,'model_called':False})
    return result


def baseline(request,session,trace):
    _,evaluation,manifest=bundle(session)
    p=profile(request['endpoint']);settings=request['settings']
    if request['schema'] is not None or settings['temperature']!=0 or settings['seed']!=42 or settings['max_tokens']!=128:
        raise ValueError('Исходная серия фиксирует temperature=0, seed=42, max_tokens=128 и отсутствие схемы генерации.')
    if settings['max_requests']<6:
        raise ValueError('Для фиксированной серии нужен max_requests не меньше 6; запросы не запускались.')
    evaluation=evaluation[:6];rows=[];started=time.monotonic()
    for index,row in enumerate(evaluation,1):
        remaining=min(settings['deadline_seconds'],180)-(time.monotonic()-started)
        if remaining<=0:raise ValueError('Бюджет серии исчерпан; неполная серия не засчитывается.')
        prompt=rendered(session,row)
        result=ModelClient(request['endpoint'],timeout=min(settings['timeout'],remaining),trace=trace).generate(prompt,max_tokens=128,temperature=0,seed=42)
        if result['metadata']['server'].get('model')!=MODEL_ID+'@'+MODEL_REVISION:raise ValueError('Во время серии изменилась модель')
        rows.append({'id':row['id'],'action':row['target']['action'],'prompt':prompt,'expected':row['target'],'output':result['text'],'generated_tokens':result['metadata']['server'].get('tokens_predicted'),**score_text(result['text'],row['target'])})
        trace.emit('evaluation_progress',completed=index,total=len(evaluation),example_id=row['id'])
    report={'format':'course-evaluation-v1','adapter':None,'settings':{'model':MODEL_ID,'revision':MODEL_REVISION,'dtype':'float32','device':'cpu','threads':4,'seed':42,'do_sample':False,'max_new_tokens':128,'eval_sha256':manifest['sha256']['eval.jsonl'],'schema_sha256':manifest['sha256']['command.schema.json'],'example_ids':[r['id'] for r in evaluation],'chat_template_sha256':p['chat_template_sha256'],'versions':p['versions']},'metrics':{key:{'count':sum(r[key] for r in rows),'total':len(rows),'rate':sum(r[key] for r in rows)/len(rows)} for key in ('valid_json','schema_valid','semantic_correct')},'examples':rows,'seconds':round(time.monotonic()-started,3)}
    return report


def last_report(session):
    bundle(session)
    cursor=0;last=None
    while True:
        batch=session.events(after=cursor,limit=100)
        if not batch:break
        for event in batch:
            if event['event']=='run_completed' and event['data'].get('kind')=='baseline' and event['data'].get('status')=='completed':last=event['data']['run_id']
        cursor=batch[-1]['sequence']
    if not last:raise ValueError('Законченной исходной серии ещё нет. Сначала baseline после разрешения преподавателя.')
    if not re.fullmatch(r'[a-f0-9]{32}',last):raise ValueError('Неверный идентификатор серии')
    envelope=json.loads(_read(session.directory/'runs'/last/'result.json',session.root,MAX_RESULT_BYTES))
    return last,envelope['result']


def report_command(session,args):
    run_id,report=last_report(session)
    if args==['freeze']:
        path=session.root/'work/training-before.json'
        # Export the same verified bundle for lesson 05, no learner shell copying.
        source,_=session._find_lesson('04');dest=session.root/'work/training-data'
        _mkdir(dest,session.root)
        _plain_path(path,session.root,missing=True)
        for name in ('train.jsonl','eval.jsonl','command.schema.json','manifest.json'):
            data=_read(source/'data'/name,session.root,MAX_RESULT_BYTES)
            target=_plain_path(dest/name,session.root,missing=True)
            if target.exists() and _read(target,session.root,MAX_RESULT_BYTES)!=data:raise ValueError('В рабочем наборе уже другие данные; они не перезаписаны.')
        if path.exists():
            previous=json.loads(_read(path,session.root,MAX_RESULT_BYTES))
            if previous!=report:raise ValueError('Исходный отчёт уже зафиксирован и не перезаписан. Обсуди другой эксперимент с преподавателем.')
        else:
            for name in ('train.jsonl','eval.jsonl','command.schema.json','manifest.json'):
                target=_plain_path(dest/name,session.root,missing=True)
                if not target.exists():target.write_bytes(_read(source/'data'/name,session.root,MAX_RESULT_BYTES))
            with path.open('x', encoding='utf-8') as stream:
                json.dump(report,stream,ensure_ascii=False,indent=2)
                stream.write('\n')
        session._record('training_baseline_frozen','student',{'run_id':run_id,'report':'work/training-before.json','settings':report['settings'],'model_called':False})
        return 'Исходный отчёт заморожен для урока 05. Веса не менялись.\nДАЛЬШЕ: F2 — обсудить, какие записи пойдут в обучение и почему eval нельзя подмешать ради красивой оценки.'
    if len(args)==1 and args[0].isdigit():
        index=int(args[0])-1
        if not 0<=index<len(report['examples']):raise ValueError('Номер ответа вне исходной серии')
        row=report['examples'][index]
        text='Сохранённый настоящий ответ №'+str(index+1)+'\nВход:\n'+row['prompt']+'\nОтвет модели:\n'+row['output']+'\nЭталон только для проверки:\n'+json.dumps(row['expected'],ensure_ascii=False)+'\nПроверки: '+json.dumps({k:row[k] for k in ('valid_json','schema_valid','semantic_correct')},ensure_ascii=False)
    elif not args:
        text='ИСХОДНОЕ ИЗМЕРЕНИЕ: '+MODEL_ID+' (отдельный локальный профиль, без адаптера).\n'+'\n'.join(f'{name}: {value["count"]}/{value["total"]}' for name,value in report['metrics'].items())+'\nЭто шесть задач, не общая оценка возможностей модели.\nДАЛЬШЕ: report 1 — разобрать первый ответ; report freeze — сохранить исходный отчёт для следующего урока.'
    else:raise ValueError('report | report 1 | report freeze')
    session._record('training_report_viewed','student',{'run_id':run_id,'args':args,'text':text,'model_called':False})
    return text
