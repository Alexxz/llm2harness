"""A visible, resumable before → pairs → actual training → after learning episode."""
import curses
import json
from pathlib import Path
import re
import subprocess
import textwrap
import threading
import time
import uuid

from .classroom import Session,MAX_TEXT,MAX_RESULT_BYTES,_read,_atomic,_plain_path
from .classroom_console import terminal_text

ACTIONS=('получить ответ ДО','подтвердить пары без обучения','начать ОБУЧЕНИЕ','получить ответ ПОСЛЕ','показать новую заявку','ответить на новую заявку','итог и возвращение в F2')

STAGES=('До обучения','Учебные пары','Обучение','Тот же запрос после','Сравнение','Новая заявка','Итог опыта')


def ensure_prepared(root):
    from tools.make_training_data import MODEL_REVISION
    root=Path(root)
    python=root/'work/training-venv/bin/python'
    weights=root/'work/model-cache/models--Qwen--Qwen2.5-0.5B-Instruct/snapshots'/MODEL_REVISION/'model.safetensors'
    if not python.is_file() or not weights.is_file():
        raise ValueError('Локальные веса или среда обучения не подготовлены. См. SETUP.md урока 04. Автоматических установок и скачиваний нет.')


def execute(request,session,trace):
    if session.snapshot()['lesson']!='04':raise ValueError('Практикум обучения доступен в уроке 04.')
    data=json.loads(request['prompt']);operation=data.get('operation');adapter_id=data.get('adapter_id')
    if operation not in {'before','train','after','probe','off'} or not isinstance(adapter_id,str) or not re.fullmatch(r'[a-f0-9]{32}',adapter_id):raise ValueError('Неверный шаг практикума')
    adapter=session.directory/'learning-adapters'/adapter_id
    _plain_path(adapter,session.root,missing=True)
    if operation in {'after','probe'}:
        for name in ('episode-manifest.json','adapter_model.safetensors','adapter_config.json'):_read(adapter/name,session.root,MAX_RESULT_BYTES)
    source=Path(__file__).resolve().parents[1]
    payload={'operation':operation,'config':data['config'],'adapter_dir':str(adapter)}
    process=subprocess.Popen([str(source/'work/training-venv/bin/python'),'-I',str(source/'tools/learning_episode_worker.py')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    result=None;size=0
    try:
        process.stdin.write(json.dumps(payload,ensure_ascii=False).encode());process.stdin.close()
        for line in process.stdout:
            size+=len(line)
            if size>512*1024:raise ValueError('Превышен предел журнала обучения')
            message=json.loads(line)
            if message['event'] in {'loading','progress'}:trace.emit('learning_progress',**{k:v for k,v in message.items() if k!='event'})
            elif message['event']=='result':result=message['result']
            elif message['event']=='error':raise ValueError('Локальный опыт не завершился: '+message.get('message',message.get('error_type','ошибка')))
        process.wait(timeout=2)
        if process.returncode or result is None:raise ValueError('Локальный worker не вернул завершённый результат; повтор только по вашему действию.')
        result['adapter_id']=adapter_id
        result['adapter_directory']=str(adapter)
        return result
    finally:
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        process.stdout.close()


class LearningLab:
    def __init__(self,parent):
        if parent.snapshot()['lesson']!='04':raise ValueError('F8 — практикум урока 04.')
        self.parent=parent;self.path=parent.directory/'learning-lab.json'
        directory,_=parent._find_lesson('04')
        config=json.loads(_read(directory/'learning-lab.json',parent.root,MAX_TEXT))
        self.state={'version':1,'stage':0,'config':config,'before':None,'training':None,'after':None,'probe':None,'off':None,'child':None,'finished':False}
        if self.path.exists():self.state=json.loads(_read(self.path,parent.root,MAX_RESULT_BYTES))
        if self.state['child']:
            identifier=self.state['child']
            if not re.fullmatch(r'lesson-04-[a-f0-9]{12}',identifier):raise ValueError('Неверная дочерняя сессия')
            self.child=Session.open(identifier,root=parent.root)
            child_state=self.child._state()
            if child_state.get('purpose')!='learning_lab' or child_state.get('parent_session')!=parent.id:raise ValueError('Сессия не принадлежит этому практикуму')
        else:
            self.child=Session.create('04',root=parent.root,endpoint=parent.snapshot()['endpoint'])
            with self.child._lock('metadata.lock'):
                child_state=self.child._state();child_state.update(purpose='learning_lab',parent_session=parent.id);self.child._save(child_state)
            self.state['child']=self.child.id
        self.owner=self.child.claim_owner();self.owner.__enter__()
        self.closed=False
        self.thread=None;self.error='';self.progress='';self.started=0;self.blocked=False
        self.cursor=0
        while True:
            events=parent.events(after=self.cursor,limit=100)
            if not events:break
            self.cursor=events[-1]['sequence']
        self.save()

    def save(self):_atomic(self.path,self.state,self.parent.root)

    @property
    def busy(self):return self.thread is not None and self.thread.is_alive()

    def edit(self,value):
        if self.busy or self.state['stage'] not in {0,1}:raise ValueError('Учебная пара уже зафиксирована для этого опыта.')
        if not value.strip() or len(value.encode())>2048:raise ValueError('Нужен текст до 2 KiB.')
        if self.state['stage']==0:
            self.state['config']['request']=value;self.state['config']['pairs'][0]['request']=value
        else:self.state['config']['pairs'][0]['answer']=value
        self.save()

    def advance(self):
        if self.busy:raise ValueError('Опыт выполняется; F6 — остановить.')
        stage=self.state['stage']
        if stage in {1,4}:
            self.state['stage']+=1;self.save();return
        if stage==6:
            if not self.state['finished']:
                self.state['finished']=True;self.save();self.parent._record('learning_lab_finished','student',{'child':self.child.id,'training':self.state['training'],'before':self.state['before'],'after':self.state['after'],'probe':self.state['probe'],'understanding_not_assessed':True})
            return 'close'
        operation={0:'before',2:'train',3:'after',5:'probe'}[stage]
        self.start(operation)

    def start(self,operation):
        if self.busy:raise ValueError('Опыт уже выполняется.')
        parent=self.parent.snapshot()
        if parent['gate']!='open' or parent['recovery_required'] or 'complete' not in parent['capabilities'] or self.blocked:raise ValueError('Запуск приостановлен преподавателем. Esc — вернуться в F2.')
        if parent['active_run']:raise ValueError('Сначала дождись опыта в основном терминале.')
        if operation in {'after','probe'} and not self.state['training']:raise ValueError('Завершённого обучения ещё нет.')
        if operation=='off' and not self.state['after']:raise ValueError('Сначала получи ответ после обучения.')
        adapter_id=uuid.uuid4().hex if operation=='train' else self.state['training']['adapter_id'] if self.state['training'] else uuid.uuid4().hex
        self.child.set_setting('deadline_seconds',180);self.child.set_setting('timeout',180)
        self.child.set_prompt(json.dumps({'operation':operation,'config':self.state['config'],'adapter_id':adapter_id},ensure_ascii=False))
        self.child._control('resume',role='student')
        self.error='';self.progress='Подготовка локального опыта';self.started=time.monotonic()
        self.parent._record('learning_lab_action','student',{'operation':operation,'child':self.child.id,'config':self.state['config']})
        def event(item):
            if item['event']=='learning_progress':
                d=item['data'];self.progress=f"Шаг {d['step']} из {d['total']} · параметры обновляются" if 'step' in d else d.get('message','Загрузка локальной модели')
                self.parent._record('learning_lab_progress','system',d)
        def work():
            try:
                result=self.child.run('learn',on_event=event)
                if result['status']!='completed':raise ValueError(result.get('error','Опыт остановлен; завершённый результат не получен.'))
                value=result['result']
                if operation=='train' and (not value.get('completed') or value.get('parameter_digest_before')==value.get('parameter_digest_after') or value.get('steps')!=self.state['config']['steps']):
                    raise ValueError('Завершённое изменение параметров не подтверждено.')
                if operation in {'after','off'} and self.state['before']:
                    before=self.state['before']
                    if any(before.get(key)!=value.get(key) for key in ('prompt','settings','base_model','base_revision')):
                        raise ValueError('Условия запроса отличаются от ДО: парное сравнение не засчитано.')
                if operation!='train':
                    from .structured import evaluate
                    directory,_=self.parent._find_lesson('04')
                    schema=json.loads(_read(directory/'core-schema.json',self.parent.root,MAX_TEXT))
                    readable_prompt='Текущая заявка: '+value['request']+'\nОтвет:\n'
                    value['check']=evaluate(value['text'],readable_prompt,schema)
                self.state[{'train':'training'}.get(operation,operation)]=value
                if operation!='off':self.state['stage']+=1
                self.save();self.parent._record('learning_lab_result','student',{'operation':operation,'child':self.child.id,'result':value,'stage':self.state['stage']})
            except Exception as error:
                self.error=str(error);self.parent._record('learning_lab_error','system',{'operation':operation,'message':self.error})
        self.thread=threading.Thread(target=work,daemon=True);self.thread.start()

    def tick(self):
        for event in self.parent.events(after=self.cursor,limit=100):
            self.cursor=event['sequence']
            if event['event'] in {'control_requested','control_applied'}:
                action=event['data'].get('action')
                if action in {'pause','stop','close'}:self.blocked=True;self.cancel()
                elif action=='resume':self.blocked=False
        return self.parent.snapshot()['gate']!='closed'

    def cancel(self):
        if self.busy:self.child._control('stop',role='student')

    def close(self):
        if self.closed:return
        self.cancel()
        if self.thread:self.thread.join(15)
        if self.busy:raise RuntimeError('Остановка опыта ещё не подтверждена; журнал сохранён.')
        self.save();self.owner.__exit__(None,None,None);self.closed=True


def teacher_view(parent):
    path=parent.directory/'learning-lab.json'
    if not path.exists():return {'opened':False,'next_action':'F8 — открыть практикум'}
    state=json.loads(_read(path,parent.root,MAX_RESULT_BYTES));stage=state['stage']
    cursor=0;visible=False
    while True:
        batch=parent.events(after=cursor,limit=100)
        if not batch:break
        for event in batch:
            if event['event']=='learning_lab_opened':visible=True
            elif event['event']=='learning_lab_closed':visible=False
        cursor=batch[-1]['sequence']
    if visible:action='F5 — '+ACTIONS[stage]
    elif state['finished']:action='Опыт закрыт. Сейчас F2: обсудить наблюдения с учеником; кнопки внутри практикума больше не предлагать.'
    else:action='Сейчас диалог F2. F8 откроет сохранённый экран «'+STAGES[stage]+'»; внутри него F5 — '+ACTIONS[stage]+'.'
    return {'opened':True,'visible':visible,'stage':stage,'stage_name':STAGES[stage],'next_action':action,'F5_inside_practice':ACTIONS[stage],'training_completed':bool(state['training']),'screen_text':content(state),'finished':state['finished'],'understanding_not_assessed_by_buttons':True}


def text_frame(title, text, width=78):
    width=max(10,width)
    inner=width-4
    heading=(' '+title+' ')[:width-2]
    lines=['┌'+heading+'─'*(width-2-len(heading))+'┐']
    for line in terminal_text(text,limit=MAX_TEXT).expandtabs(4).split('\n'):
        for part in textwrap.wrap(line,width=inner,replace_whitespace=False,drop_whitespace=False) or ['']:
            lines.append('│ '+part.ljust(inner)+' │')
    lines.append('└'+'─'*(width-2)+'┘')
    return lines


def content(state,details=False,width=78):
    c=state['config'];stage=state['stage']
    chapters=(1,2,3,4,4,5,5)
    titles=('Ответ до обучения','Учебные пары','Настоящее обучение','Ответ после обучения','Сравнение до и после','Новая заявка','Итог опыта')
    goals=(
        'Обработчику заказов нужен чистый JSON с верными напитком и количеством. Сначала проверим, подходит ли исходный ответ. Затем попробуем обучением закрепить нужный формат и повторим тот же запрос.',
        'Учебная пара — заявка и наш желаемый ответ. Программа будет учиться на этих образцах; сейчас мы только рассматриваем их, параметры пока не меняются.',
        'Параметры — числа, с помощью которых модель выбирает продолжение текста. Программа сравнит предсказания с учебными образцами и вычислит поправки к этим числам. Здесь обучается только небольшая добавка LoRA; исходные веса остаются прежними.',
        'Теперь подключим обученную добавку и повторим прежний запрос. Учебных пар в нём не будет: проверяем влияние изменённых параметров, а не подсказки в тексте.',
        'Запрос и настройки одинаковые; отличается подключение добавки. Сравним форму и содержание ответов отдельно: правильный JSON ещё может содержать неверный заказ.',
        'Знакомая заявка участвовала в обучении, поэтому успех на ней мог быть подстройкой под пример. Новая заявка покажет, получилось ли применить выученное хотя бы в одном другом случае.',
        'Обучение изменило параметры добавки, а три ответа показывают результат на двух заявках. Обсудим, помогла ли добавка с формой и сведениями о заказе. Для вывода о надёжности понадобятся другие заявки.')
    done=(
        'Ещё ничего не запускали. Параметры не менялись.',
        'Получен ответ ДО. Обучения ещё не было.',
        'Четыре учебные пары подтверждены. Обучения ещё не было.',
        'Обучение завершено. Параметры добавки изменились.',
        'Получены оба ответа на один и тот же вход.',
        'Сравнение на знакомой заявке сохранено.',
        'Получен ответ и на новую заявку. Результаты сохранены.')
    actions=(
        'F5 — получить ответ исходной модели.',
        'Сверь каждую заявку с образцом: напиток передан в drink, количество — числом в cups, вокруг JSON нет пояснений. F5 — подтвердить пары без запуска обучения.',
        'F5 — запустить настоящее обучение.',
        'F5 — отправить тот же вход с обученной добавкой.',
        'Сравни JSON и сведения о заказе в двух ответах. F5 — показать новую заявку.',
        'F5 — получить ответ на новую заявку с добавкой.',
        'F5 — вернуться в F2 и обсудить наблюдения с преподавателем.')
    results=(
        'Появится настоящий ответ ДО, затем откроются учебные пары.',
        'Откроется экран запуска обучения. До отдельного нажатия веса не меняются.',
        'Появятся шаги и время обучения. После завершения можно будет повторить запрос.',
        'Появится ответ ПОСЛЕ и сравнение рядом с исходным.',
        'Появится другая заявка. Её ответ пока не будет запрошен.',
        'Появится ответ. Проверим, верно ли указаны напиток и число чашек.',
        'В F2 разберём, что изменилось в ответах и какие вопросы остались.')
    lines=[f'ЭТАП {chapters[stage]}/5 · {titles[stage]}',
           '1 Ответ до  →  2 Пары  →  3 Обучение  →  4 Сравнение  →  5 Новая заявка',
           '', 'ЦЕЛЬ',goals[stage],'','УЖЕ СДЕЛАНО',done[stage],'','ТЕКУЩИЕ ДАННЫЕ']
    def answer(key,label):
        value=state.get(key)
        if not value:return
        lines.extend(text_frame('Выход · '+label,value['text'],width))
        if value.get('check'):
            check=value['check'];status=lambda v:'да' if v is True else 'нет' if v is False else 'не проверяли'
            lines.append('JSON читается: '+status(check['json_valid'])+' | Поля/типы: '+status(check['schema_valid'])+' | Значения: '+status(check['content_correct']))
    if stage==0:
        lines+=['ЧТО ПОЛУЧИТ МОДЕЛЬ',
                'Запрос состоит из двух частей: инструкции о формате и заявки на напиток.']
        lines+=text_frame('Вход · инструкция модели',c['instruction'],width)
        lines+=text_frame('Вход · заявка пользователя',c['request'],width)
        lines+=['','ПОЯСНЕНИЕ ДЛЯ ТЕБЯ · МОДЕЛИ НЕ ОТПРАВЛЯЕТСЯ',
                'Для другой заявки «Одна чашка кофе» нужный ответ выглядел бы так:']
        lines+=text_frame('Пример выхода · не ответ модели','{"drink":"coffee","cups":1}',width)
        lines+=['Это иллюстрация формата: drink хранит напиток, cups — число чашек.',
                '', 'РЕАЛЬНАЯ ОШИБКА · ИЗ ПРОШЛОГО ПРОГОНА',
                'На заявку «Две чашки чая» исходная модель уже отвечала так:']
        lines+=text_frame('Выход · сохранённый ответ до обучения','```json\n{\n  "drink": "tea",\n  "cups": 2\n}\n```',width)
        lines+=['Данные внутри верны, но строки с ``` вокруг объекта не входят в JSON.',
                'Наш обработчик читает весь ответ как JSON и останавливается на первой строке.',
                'Инструкция «без пояснений и Markdown» уже была в запросе.',
                'Попробуем обучением закрепить ответ без обрамления. Успех не гарантирован.',
                'F5 — получить ответ на текущую заявку.',
                'F9 — посмотреть пример в песочнице: точный вход и настройки уже заполнены.',
                'В песочнице F5 получает новый ответ, Esc возвращает на этот шаг.']
    elif stage==1:
        lines+=['Это подготовленные нами образцы, а не новые ответы модели.',
                'Как читать пару: «Две чашки чая» → {"drink":"tea","cups":2}.',
                '«чая» задаёт drink=tea, «две» — cups=2. Число записано без кавычек.',
                'В остальных парах меняются напиток и количество, но сохраняется форма: только JSON с drink и cups.',
                'Программа будет учиться по этим ответам, поэтому сначала проверяем сами образцы.']
        for i,pair in enumerate(c['pairs'],1):
            lines+=text_frame(f'{i}. Вход · заявка',pair['request'],width)
            lines+=text_frame(f'{i}. Выход · Желаемый ответ:',pair['answer'],width)
            lines.append('')
        lines+=['F7 — при желании изменить первый образец. Полный ответ ДО — в F4.']
    elif stage==2:
        lines+=[f"Учебных пар: {len(c['pairs'])}. Шагов обновления: {c['steps']}."]
    elif stage==3:
        t=state['training'];lines+=[f"Выполнено {t['steps']} шагов за {t['training_seconds']:.1f} с обучения.",
            'Инструкция и настройки те же. Учебные образцы в запрос не добавляются.']
        lines+=text_frame('Вход · повторная заявка',c['request'],width)
    elif stage==4:
        lines+=text_frame('Вход · одинаковый для обоих ответов',c['request'],width)
        answer('before','А. БЕЗ ОБУЧЕННОЙ ДОБАВКИ');lines.append('')
        answer('after','Б. С ОБУЧЕННОЙ ДОБАВКОЙ')
        if state.get('off'):
            lines.append('');answer('off','Дополнительная проверка: ДОБАВКА ОТКЛЮЧЕНА')
        else:lines+=['','Необязательно: F8 повторит эту заявку без добавки.']
        lines+=['Изменение параметров не гарантирует улучшение каждого ответа.']
    elif stage==5:
        lines+=text_frame('Вход · новая заявка',c['new_request'],width)
        lines+=['Этой заявки не было в четырёх учебных парах.',
                'Правильный ответ не будет подставлен в запрос.']
    else:
        answer('before','1. ДО:');lines.append('');answer('after','2. ПОСЛЕ:');lines.append('');answer('probe','3. НОВАЯ ЗАЯВКА:')
        lines+=['','Учебные пары называют train, отдельные проверочные задачи — eval.',
                'Успех на знакомой заявке не доказывает надёжность на всех новых.']
    lines+=['','ВАШЕ ДЕЙСТВИЕ',actions[stage],'','ПОСЛЕ ЭТОГО',results[stage]]
    if details:
        lines+=['','ДЕТАЛИ ОПЫТА · F4 вернёт основной экран']
        for key,label in [('before','ДО'),('after','ПОСЛЕ'),('off','ДОБАВКА ОТКЛЮЧЕНА'),('probe','НОВАЯ ЗАЯВКА')]:
            value=state.get(key)
            if value:
                lines+=['',label]
                lines+=text_frame('Вход · заявка',value['request'],width)
                lines+=text_frame('Выход · полный ответ',value['text'],width)
                lines+=text_frame('Вход · точный запрос модели',value['prompt'],width)
                lines+=['Настройки: '+json.dumps(value['settings'],ensure_ascii=False)]
        if state.get('training'):
            t=state['training'];lines+=['','Параметры опыта: '+json.dumps({k:c[k] for k in ('steps','rank','learning_rate','max_new_tokens')},ensure_ascii=False),
                'Файл добавки: '+t.get('adapter_directory','сохранён в дочерней сессии'),
                'Изменение обучаемых параметров:',t['parameter_digest_before']+' → '+t['parameter_digest_after'],
                f"Обучаемых чисел: {t['trainable_parameters']}"]
    return '\n'.join(lines)


def open_example(screen, lab):
    from .playground import Playground, _screen as playground_screen
    from .training_lesson import profile
    if lab.busy:
        raise ValueError('Дождись завершения текущего опыта.')
    if lab.parent.snapshot()['endpoint'].rstrip('/') != 'http://127.0.0.1:8089':
        raise ValueError('Для примера нужен локальный профиль урока 04 на порту 8089.')
    profile()
    directory, _ = lab.parent._find_lesson('04')
    prompt = _read(directory / 'prompts/reproduce-before.txt', lab.parent.root, MAX_TEXT).decode('utf-8')
    playground = Playground(lab.parent, preset={'prompt': prompt, 'max_tokens': 64, 'temperature': 0, 'seed': 42})
    try:
        return playground_screen(screen, playground)
    finally:
        playground.close()
        lab.parent._record('playground_closed', 'student', {'example': 'lesson04-before'})
        screen.timeout(100)


def laboratory_menu(state):
    return '\n'.join([
        'ЛАБОРАТОРИЯ · УРОК 04', '',
        'Объяснение урока — слева, в F2.',
        'Здесь можно выбрать подготовленный опыт. Выбор ничего не запускает.', '',
        'F9 · Пример ошибки',
        'Точный запрос, на котором модель добавляла Markdown вокруг JSON.',
        'Откроется песочница; F5 внутри неё получает настоящий ответ.', '',
        'F8 · Обучение: до и после',
        'Заявка → учебные пары → обучение → сравнение ответов.',
        'Откроется практикум с сохранённого шага: '+STAGES[state['stage']]+'.', '',
        'F2 — к объяснению преподавателя',
    ])


def _laboratory_screen(screen, lab):
    screen.timeout(100)
    message=''
    while lab.tick():
        try:curses.curs_set(0)
        except curses.error:pass
        screen.erase()
        rows, cols=screen.getmaxyx()
        lines=[]
        for line in (laboratory_menu(lab.state)+'\n'+message).splitlines():
            lines+=textwrap.wrap(line,width=max(10,cols-2)) or ['']
        for row,line in enumerate(lines[:max(1,rows-1)]):
            try:screen.addnstr(row,0,line,max(1,cols-1))
            except curses.error:pass
        screen.refresh()
        try:key=screen.get_wch()
        except curses.error:continue
        try:
            if key==curses.KEY_F9:
                open_example(screen,lab)
            elif key in (curses.KEY_F8,'\n','\r'):
                lab.parent._record('learning_lab_opened','student',{'child':lab.child.id})
                try:_screen(screen,lab)
                finally:lab.parent._record('learning_lab_closed','student',{'stage':lab.state['stage'],'finished':lab.state['finished']})
            elif key=='\x1b':
                subprocess.run(['tmux','select-pane','-L'],check=False,timeout=3)
        except (ValueError,RuntimeError,OSError) as error:message=str(error)
    return 0


def run_lab(parent, *, embedded=False):
    lab=LearningLab(parent);lab.embedded=embedded
    if not embedded:parent._record('learning_lab_opened','student',{'child':lab.child.id})
    try:return curses.wrapper(lambda screen:(_laboratory_screen if embedded else _screen)(screen,lab))
    finally:
        lab.close();parent._record('learning_lab_closed','student',{'stage':lab.state['stage'],'finished':lab.state['finished']})


def _screen(screen,lab):
    import textwrap
    screen.timeout(100);offset=0;details=False;editing=False;draft='';position=0;selected=False;message='F5 выполняет подписанный шаг. Esc — назад; F2 — к преподавателю.'
    while lab.tick():
        rows,cols=screen.getmaxyx();screen.erase()
        def put(y,text,style=0):
            try:screen.addnstr(y,0,terminal_text(text,limit=MAX_TEXT),max(1,cols-1),style)
            except curses.error:pass
        wrapped=[]
        for line in content(lab.state,details,width=max(10,cols-2)).splitlines():wrapped+=textwrap.wrap(line,width=max(10,cols-2),replace_whitespace=False) or ['']
        height=max(1,rows-5);offset=min(max(0,offset),max(0,len(wrapped)-height))
        for i,line in enumerate(wrapped[offset:offset+height]):
            heading=line in {'ЦЕЛЬ','УЖЕ СДЕЛАНО','ТЕКУЩИЕ ДАННЫЕ','ВАШЕ ДЕЙСТВИЕ','ПОСЛЕ ЭТОГО','ЧТО ПОЛУЧИТ МОДЕЛЬ','ПОЯСНЕНИЕ ДЛЯ ТЕБЯ · МОДЕЛИ НЕ ОТПРАВЛЯЕТСЯ'} or line.startswith(('ЭТАП ','А. ','Б. ','ДЕТАЛИ ОПЫТА'))
            put(i,line,curses.A_BOLD if heading else 0)
        if editing:
            put(rows-4,message)
            left=max(0,position-cols+3);put(rows-3,draft[left:],curses.A_REVERSE if selected else 0)
        elif lab.busy:
            put(rows-3,'|/-\\'[int(time.monotonic()*8)%4]+f' {int(time.monotonic()-lab.started)} с · '+lab.progress,curses.A_BOLD)
        else:put(rows-3,lab.error or message)
        put(rows-2,'↑↓ / PgUp PgDn — читать · F4 — детали · Esc — назад · F2 — преподаватель')
        labels=ACTIONS
        footer='Выполняется · F6 остановить · Esc остановить и вернуться' if lab.busy else 'F5 — '+labels[lab.state['stage']]+' · F7 изменить до обучения'
        if not lab.busy and lab.state['stage']==0:footer+=' · F9 пример в песочнице'
        put(rows-1,footer,curses.A_REVERSE)
        try:
            curses.curs_set(1 if editing else 0)
            if editing:screen.move(rows-3,min(cols-2,position-max(0,position-cols+3)))
            key=screen.get_wch()
        except curses.error:continue
        if editing:
            if key=='\x1b':editing=False
            elif key in ('\n','\r'):
                try:lab.edit(draft);editing=False;offset=0
                except ValueError as error:message=str(error)
            elif key==curses.KEY_LEFT:position=max(0,position-1);selected=False
            elif key==curses.KEY_RIGHT:position=min(len(draft),position+1);selected=False
            elif key in (curses.KEY_HOME,'\x01'):position=0;selected=False
            elif key in (curses.KEY_END,'\x05'):position=len(draft);selected=False
            elif key in (curses.KEY_BACKSPACE,'\x7f','\b'):
                if selected:draft='';position=0;selected=False
                elif position:draft=draft[:position-1]+draft[position:];position-=1
            elif key==curses.KEY_DC:draft=draft[:position]+draft[position+1:];selected=False
            elif key=='\x15':draft='';position=0;selected=False
            elif isinstance(key,str) and key.isprintable():
                if selected:draft='';position=0;selected=False
                candidate=draft[:position]+key+draft[position:]
                if len(candidate.encode())<=2048:draft=candidate;position+=1
            continue
        if key=='\x1b':return 0
        if key in (curses.KEY_DOWN,curses.KEY_NPAGE):offset+=1 if key==curses.KEY_DOWN else height
        elif key in (curses.KEY_UP,curses.KEY_PPAGE):offset-=1 if key==curses.KEY_UP else height
        elif key==curses.KEY_F4:details=not details;offset=0
        elif key==curses.KEY_F6:lab.cancel();message='Запрошена остановка текущего опыта.'
        elif key==curses.KEY_F7 and not lab.busy and lab.state['stage'] in {0,1}:
            editing=True;draft=lab.state['config']['request'] if lab.state['stage']==0 else lab.state['config']['pairs'][0]['answer'];position=len(draft);selected=True
            message='Редактирование: стрелки · новый ввод заменяет выделенное · Enter сохранить · Esc отменить'
        elif key==curses.KEY_F9 and lab.state['stage']==0 and not lab.busy:
            try:
                open_example(screen,lab)
                message='Пример закрыт. Ты на прежнем шаге практикума; F5 — получить ответ ДО.'
            except (ValueError,RuntimeError,OSError) as error:message=str(error)
        elif key==curses.KEY_F8 and lab.state['stage']>=4:
            try:lab.start('off');offset=0
            except ValueError as error:message=str(error)
        elif key==curses.KEY_F5:
            try:
                if lab.advance()=='close':return 0
                offset=0
            except (ValueError,RuntimeError) as error:message=str(error)
    return 0
