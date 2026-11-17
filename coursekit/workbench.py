"""One terminal workspace for lesson requests and real adapter training."""
import copy
import curses
import json
import subprocess
import threading
import time
import uuid

from .classroom import MAX_TEXT, MAX_RESULT_BYTES, _atomic, _read, _setting
from .classroom_console import terminal_text
from .playground import prompt_rows

PARAMETERS = ('max_tokens', 'temperature', 'seed')
HELP = 'F1 помощь · F2 преподаватель · F3 лаборатория · F4 пресеты · F5 выполнить · F6 стоп · F7 режим · F8 модель · F9 история в «Вход модели» · Tab поле'


def definition(session):
    directory, _ = session._find_lesson(session.snapshot()['lesson'])
    return json.loads(_read(directory / 'workspace.json', session.root, MAX_TEXT))


def phase_index(session):
    index = 0
    cursor = 0
    while True:
        batch = session.events(after=cursor, limit=100)
        if not batch:
            return index
        for item in batch:
            if item['event'] == 'workspace_phase':
                index = item['data']['index']
        cursor = batch[-1]['sequence']


def advance_phase(session):
    spec = definition(session)
    index = min(phase_index(session) + 1, len(spec['phases']) - 1)
    session._record('workspace_phase', 'teacher', {'index': index, 'name': spec['phases'][index]['name']})


def parse_pairs(text):
    pairs = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if ' -> ' not in line:
            raise ValueError(f'Строка {number}: раздели вход и ответ пробелами и стрелкой « -> » .')
        request, answer = line.split(' -> ', 1)
        if not request.strip() or not answer.strip() or max(len(request.encode()), len(answer.encode())) > 2048:
            raise ValueError(f'Строка {number}: нужны непустые вход и ответ до 2 KiB каждый.')
        pairs.append({'request': request.strip(), 'answer': answer.strip()})
    if not 1 <= len(pairs) <= 16:
        raise ValueError('Нужно от 1 до 16 примеров: один пример на строку, вход -> выход.')
    return pairs


def teacher_view(session):
    path = session.directory / 'workspace.json'
    if not path.exists():
        return None
    state = json.loads(_read(path, session.root, MAX_RESULT_BYTES))
    spec = definition(session)
    state['phase_index'] = phase_index(session)
    state['phase'] = spec['phases'][state['phase_index']]
    state['phases'] = spec['phases']
    state['presets'] = spec['presets']
    state['keys'] = HELP
    from .lesson_flow import progress
    state['teaching'] = progress(session)
    return state


class Workspace:
    def __init__(self, session):
        self.session = session
        self.spec = definition(session)
        self.path = session.directory / 'workspace.json'
        snapshot = session.snapshot()
        self.state = {'version': 1, 'mode': 'request', 'model': 'original', 'preset': None,
                      'prompt': snapshot['prompt'], 'parameters': {k: snapshot['settings'][k] for k in PARAMETERS},
                      'pairs': '', 'adapters': [], 'output': '', 'result': None}
        if self.path.exists():
            self.state = json.loads(_read(self.path, session.root, MAX_RESULT_BYTES))
        if snapshot['lesson'] == '04' and not self.state['pairs']:
            self.state['pairs'] = '\n'.join(p['request'] + ' -> ' + p['answer'] for p in self.spec['training']['pairs'])
        self.thread = None
        self.error = self.state.get('error', '')
        self.progress = self.state.get('progress', '')
        self.state['running'] = False
        self.state['interface'] = {'panel': 'workspace'}
        self.save('opened')

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    @property
    def phase(self):
        return self.spec['phases'][phase_index(self.session)]

    def save(self, action=None):
        _atomic(self.path, self.state, self.session.root)
        if action:
            self.session._record('workspace_state', 'student', {'action': action, 'state': copy.deepcopy(self.state)})

    def panel(self, name, selected=None):
        self.state['interface'] = {'panel': name}
        if selected is not None:
            self.state['interface']['selected_preset'] = selected
        self.save('panel_changed')

    def load(self, identifier):
        if self.busy:
            raise ValueError('Дождись завершения опыта или останови его.')
        if identifier not in self.phase['presets']:
            raise ValueError('Этот пресет откроется в следующей фазе урока.')
        preset = next(p for p in self.spec['presets'] if p['id'] == identifier)
        self.state.update(prompt=self.session._asset(preset['prompt_file']), parameters=dict(preset['parameters']), preset=identifier)
        self.state.update(output='',result=None,check=None,last_input=None)
        self.state.setdefault('results_by_mode', {}).pop('request', None)
        self.session.set_prompt(self.state['prompt'])
        self.error = ''
        self.state['error'] = ''
        self.save('preset_loaded')

    def switch_mode(self):
        if self.busy:
            raise ValueError('Сначала останови текущий опыт.')
        if not self.phase.get('training'):
            raise ValueError('Дообучение пока закрыто уроком.')
        fields = ('output', 'result', 'check', 'last_input')
        saved = self.state.setdefault('results_by_mode', {})
        saved[self.state['mode']] = copy.deepcopy({key: self.state.get(key) for key in fields})
        self.state['mode'] = 'training' if self.state['mode'] == 'request' else 'request'
        restored = saved.get(self.state['mode'], {'output': '', 'result': None, 'check': None, 'last_input': None})
        self.state.update(copy.deepcopy(restored))
        self.error = ''
        self.state['error'] = ''
        self.save('mode_changed')

    def switch_model(self):
        if self.state['mode'] != 'request':
            raise ValueError('Модель выбирается в режиме «Запрос». Здесь обучается новая добавка к исходной модели.')
        if self.busy:
            raise ValueError('Сначала останови текущий опыт.')
        models = ['original'] + [a['adapter_id'] for a in self.state['adapters']]
        self.state['model'] = models[(models.index(self.state['model']) + 1) % len(models)]
        self.save('model_changed')

    def assemble(self):
        if not self.phase.get('history') or self.busy:
            raise ValueError('Сборка истории сейчас недоступна.')
        from .prompt_editor import history_text
        self.state['prompt'], _ = history_text(self.session)
        self.session.set_prompt(self.state['prompt'])
        self.save('history_assembled')

    def start(self):
        if self.busy:
            raise ValueError('Опыт уже выполняется.')
        snapshot = self.session.snapshot()
        if not self.phase.get('requests', True):
            raise ValueError('Запуск приостановлен: сначала заверши знакомство с интерфейсом.')
        if snapshot['gate'] != 'open' or snapshot['recovery_required'] or 'complete' not in snapshot['capabilities']:
            raise ValueError('Запуск приостановлен преподавателем.')
        parameters = {}
        for key in PARAMETERS:
            try:
                value = float(str(self.state['parameters'][key]).replace(',', '.')) if key == 'temperature' else int(self.state['parameters'][key])
            except (TypeError, ValueError):
                raise ValueError(f'{key}: нужно корректное число.') from None
            _setting(key, value, {'complete'})
            if key not in self.phase['parameters'] and value != next((p for p in self.spec['presets'] if p['id'] == self.state['preset']), self.spec['presets'][0])['parameters'][key]:
                raise ValueError(f'Изменение {key} пока закрыто уроком.')
            parameters[key] = value
        if self.spec.get('training') and parameters['max_tokens'] > 128:
            raise ValueError('Для локального профиля обучения max_tokens не больше 128.')
        mode = self.state['mode']
        if mode == 'training' and not self.phase.get('training'):
            raise ValueError('Дообучение пока закрыто уроком.')
        prompt = self.state['prompt']
        if not prompt.strip() or len(prompt.encode()) > MAX_TEXT:
            raise ValueError('Нужен непустой вход до 64 KiB.')
        for key, value in parameters.items():
            self.session.set_setting(key, value)
        kind, payload = 'complete', prompt
        if self.spec.get('training'):
            config = copy.deepcopy(self.spec['training'])
            config.update(raw_prompt=prompt, request=prompt, max_new_tokens=parameters['max_tokens'], temperature=parameters['temperature'], seed=parameters['seed'])
            selected = next((a for a in self.state['adapters'] if a['adapter_id'] == self.state['model']), None)
            if mode == 'training':
                config['pairs'] = parse_pairs(self.state['pairs'])
                operation, adapter_id = 'train', uuid.uuid4().hex
            else:
                operation = 'after' if selected else 'before'
                adapter_id = selected['adapter_id'] if selected else uuid.uuid4().hex
                if selected:
                    config['pairs'] = selected['pairs']
            kind = 'learn'
            payload = json.dumps({'operation': operation, 'adapter_id': adapter_id, 'config': config}, ensure_ascii=False)
            self.session.set_setting('deadline_seconds', 180)
            self.session.set_setting('timeout', 180)
        self.session.set_prompt(prompt)
        self.state['last_input'] = {'prompt': prompt, 'parameters': parameters, 'model': self.state['model'], 'mode': mode}
        self.state['output'] = ''
        self.state['check'] = None
        self.state['result'] = None
        self.error, self.progress = '', 'Выполняется'
        self.state.update(error='', progress=self.progress, running=True)
        self.save('started')

        def event(item):
            if item['event'] == 'learning_progress':
                data = item['data']
                self.progress = f"Шаг {data['step']} из {data['total']}" if 'step' in data else data.get('message', 'Загрузка модели')
                self.state['progress'] = self.progress
                self.save('progress')

        def work():
            try:
                result = self.session.run(kind, input_text=payload, on_event=event)
                if result['status'] != 'completed':
                    raise ValueError(result.get('error', 'Опыт остановлен: ' + result['status']))
                value = result['result']
                self.state['result'] = value
                if mode == 'training':
                    if not value.get('completed') or value['parameter_digest_before'] == value['parameter_digest_after']:
                        raise ValueError('Изменение параметров добавки не подтверждено.')
                    self.state['adapters'].append(value)
                    self.state['output'] = f"Добавка №{len(self.state['adapters'])} обучена. Шагов: {value['steps']}. Переключись в «Запрос» и выбери её через F8."
                else:
                    self.state['output'] = value['text']
                    if self.phase.get('check'):
                        from .structured import evaluate
                        directory, _ = self.session._find_lesson(snapshot['lesson'])
                        schema = json.loads(_read(directory / 'core-schema.json', self.session.root, MAX_TEXT))
                        check_prompt = prompt
                        if snapshot['lesson'] == '04':
                            import re
                            match = re.search(r'<\|im_start\|>user\n(.*?)<\|im_end\|>\n<\|im_start\|>assistant\n?$', prompt, re.S)
                            if match:
                                check_prompt = 'Текущая заявка: ' + match[1] + '\nОтвет:\n'
                        check = evaluate(value['text'], check_prompt, schema)
                        self.state['check'] = check
                        self.session._record('workspace_check', 'system', {'run_id': result['run_id'], 'check': check})
            except Exception as error:
                self.error = str(error)
            finally:
                self.state.update(error=self.error, progress='Завершено' if not self.error else 'Не завершено', running=False)
                self.save('completed')
        self.thread = threading.Thread(target=work, name='workspace-run', daemon=True)
        self.thread.start()

    def cancel(self):
        if self.busy:
            self.session._control('stop', role='student')

    def close(self):
        self.cancel()
        if self.thread:
            self.thread.join(15)
        if self.busy:
            raise RuntimeError('Остановка ещё не подтверждена; проверь журнал.')
        self.save('closed')


def run_workspace(session):
    with session.claim_owner():
        workspace = Workspace(session)
        try:
            return curses.wrapper(lambda screen: _screen(screen, workspace))
        finally:
            workspace.close()


def _screen(screen, lab):
    screen.timeout(150)
    focus, position, output_offset = 0, 0, 0
    menu, menu_index, help_open = False, 0, False
    message = 'F4 — выбрать пресет. Загрузка не запускает модель.'
    pending = False
    selected = False
    last_edit = 0
    while lab.session.snapshot()['gate'] != 'closed':
        rows, cols = screen.getmaxyx()
        screen.erase()
        cursor = None
        def put(y, text, style=0):
            if 0 <= y < rows:
                try:
                    screen.addnstr(y, 0, terminal_text(text, limit=MAX_TEXT), max(1, cols-1), style)
                except curses.error:
                    pass
        def frame(top, bottom, title):
            width = cols-1
            label = (' ' + title + ' ')[:max(0, width-2)]
            put(top, '┌' + label + '─' * max(0, width-2-len(label)) + '┐', curses.A_BOLD)
            for row in range(top+1, bottom):
                put(row, '│' + ' ' * max(0, width-2) + '│')
            put(bottom, '└' + '─' * max(0, width-2) + '┘')
        if pending and time.monotonic() - last_edit > .5:
            lab.save('edited')
            pending = False
        training = lab.state['mode'] == 'training'
        name = 'pairs' if training else 'prompt'
        text = lab.state[name]
        position = min(position, len(text if focus == 0 else str(lab.state['parameters'][PARAMETERS[focus-1]])))
        phase = lab.phase
        model = 'Исходная модель' if lab.state['model'] == 'original' else 'Модель + добавка №' + str(next(i+1 for i,a in enumerate(lab.state['adapters']) if a['adapter_id'] == lab.state['model']))
        put(0, 'ЛАБОРАТОРИЯ · ' + ('ДООБУЧЕНИЕ' if training else 'ЗАПРОС'), curses.A_BOLD)
        put(1, phase['name'] + ' · ' + ('Новая добавка к исходной модели' if training else model))
        current_preset = next((p for p in lab.spec['presets'] if p['id'] == lab.state['preset']), None)
        title = current_preset['title'] + ' [' + current_preset['id'] + ']' if current_preset else 'подготовленный Вход модели'
        put(2, 'Пресет: ' + title + ' · F4 выбрать')
        if rows < 22 or cols < 46:
            put(4, 'Увеличь область лаборатории хотя бы до 46×22.')
        elif help_open:
            for i, line in enumerate(HELP.split(' · ')):
                put(4+i, line)
            put(16, 'Ввод: стрелки, Home/End, Enter — новая строка, Ctrl+u — очистить.')
            if phase.get('training'):
                put(17, 'Пары: один пример на строку, вход -> желаемый выход.')
            put(18, 'F1 / Esc — закрыть помощь. Предыдущие опыты видит преподаватель.')
        else:
            extra = 0
            if menu:
                count = min(3, len(lab.spec['presets']))
                extra = count + 1
                put(3, f'ПРЕСЕТЫ · {menu_index+1}/{len(lab.spec["presets"])} · ↑↓ выбрать · Enter загрузить · Esc отменить', curses.A_BOLD)
                first = max(0, min(menu_index-1, len(lab.spec['presets'])-count))
                for row, preset in enumerate(lab.spec['presets'][first:first+count]):
                    label = '[' + preset['id'] + ']'
                    if preset['id'] not in phase['presets']:
                        label += ' (позже)'
                    label += ' ' + preset['title']
                    put(4+row, ('› ' if first+row == menu_index else '  ') + label, curses.A_REVERSE if first+row == menu_index else 0)
            input_top = 5 + extra
            height = max(3, (rows-15-extra)//2)
            input_title = 'Примеры · вход -> желаемый выход' if training else 'Вход модели'
            if focus == 0 and not menu and not lab.busy:
                input_title += ' · редактирование'
            frame(input_top-1, input_top+height, input_title)
            visual = prompt_rows(text, max(1, cols-5))
            line = max((i for i,(start,_) in enumerate(visual) if start <= position), default=0) if focus == 0 else 0
            top = max(0, line-height+1)
            for i, (_, value) in enumerate(visual[top:top+height]):
                put(input_top+i, '│ ' + value)
            sy = input_top+height
            for i, key in enumerate(PARAMETERS, 1):
                label = f"{key}: [{lab.state['parameters'][key]}]"
                label += ' [закрыто уроком]' if key not in phase['parameters'] else ''
                put(sy+i, label, curses.A_REVERSE if focus == i else 0)
            if training:
                c = lab.spec['training']
                output_title = f"Результат обучения · {c['steps']} шагов · rank={c['rank']} · lr={c['learning_rate']}"
            else:
                previous = lab.state.get('last_input')
                changed = previous and (previous['prompt'] != lab.state['prompt'] or previous['model'] != lab.state['model'] or previous['parameters'] != lab.state['parameters'] or previous['mode'] != lab.state['mode'])
                output_title = 'Выход модели' + (' · предыдущий запуск' if changed else '') + (' · ' + lab.progress if lab.busy else '')
            frame(sy+4, rows-4, output_title)
            output_text = lab.state['output']
            output = prompt_rows(output_text, max(1, cols-5))
            room = max(1, rows-sy-9)
            output_offset = max(0, min(output_offset, max(0,len(output)-room)))
            for i, (_, value) in enumerate(output[output_offset:output_offset+room]):
                put(sy+5+i, '│ ' + value)
            if not menu and not lab.busy:
                if focus == 0:
                    cursor = (input_top+line-top, min(cols-3, 2+position-visual[line][0]))
                else:
                    cursor = (sy+focus, min(cols-3, len(PARAMETERS[focus-1])+3+position))
        check = lab.state.get('check')
        status = message
        if not training and not lab.busy and lab.state.get('result') is not None and lab.state['output'] == '':
            status = 'Запрос завершён. Модель вернула пустой ответ.'
        if check and not training:
            def verdict(value):
                return 'да' if value is True else 'нет' if value is False else 'не проверено'
            status = 'JSON: ' + verdict(check['json_valid']) + ' · поля: ' + verdict(check['schema_valid']) + ' · смысл: ' + verdict(check['content_correct'])
        if menu:
            status = 'Пресет заполнит «Вход модели» и параметры без запроса.'
        put(rows-3, lab.error or (lab.progress if lab.busy else status))
        put(rows-2, 'F4 / Esc закрыть · ↑↓ выбрать · Enter загрузить' if menu else 'Tab поле · стрелки курсор · Enter новая строка · F1 помощь')
        if menu:
            footer = 'Enter загрузить · Esc отменить · F6 остановить'
        elif help_open:
            footer = 'F1 / Esc закрыть справку · F6 остановить'
        elif lab.busy:
            footer = 'Выполняется · F6 остановить · F2 преподаватель'
        else:
            snapshot = lab.session.snapshot()
            if 'complete' not in snapshot['capabilities'] or not phase.get('requests', True):
                launch = 'Запуск модели в этом уроке закрыт'
            elif snapshot['gate'] != 'open':
                launch = 'F5 запуск приостановлен преподавателем'
            else:
                launch = 'F5 ' + ('обучить добавку' if training else 'запрос')
            footer = launch + (' · F7 режим' if phase.get('training') else ' · F7 обучение позже')
            if not training:
                footer += ' · F8 модель'
        put(rows-1, footer, curses.A_REVERSE)
        # Footer drawing also moves the curses cursor. Restore the edit position last.
        try:
            curses.curs_set(1 if cursor is not None else 0)
            if cursor is not None:
                screen.move(*cursor)
        except curses.error:
            pass
        screen.refresh()
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        try:
            if key == curses.KEY_F6:
                was_busy = lab.busy
                lab.cancel()
                message = 'Запрошена остановка текущего опыта.' if was_busy else 'Сейчас нет выполняющегося запроса или обучения.'
            elif key == curses.KEY_F3:
                lab.save('focused')
            elif key == curses.KEY_F2:
                lab.save('teacher_focus')
                subprocess.run(['tmux', 'select-pane', '-L'], timeout=3, check=False)
            elif key == curses.KEY_F1:
                help_open = not help_open
                if help_open:
                    menu = False
                lab.panel('help' if help_open else 'workspace')
                lab.save('help_opened' if help_open else 'help_closed')
            elif help_open:
                if key == '\x1b':
                    help_open = False
                    lab.panel('workspace')
                    lab.save('help_closed')
            elif menu:
                if key in ('\x1b', curses.KEY_F4):
                    menu = False
                    lab.panel('workspace')
                elif key in (curses.KEY_UP, curses.KEY_DOWN):
                    menu_index = (menu_index + (1 if key == curses.KEY_DOWN else -1)) % len(lab.spec['presets'])
                    lab.panel('presets', lab.spec['presets'][menu_index]['id'])
                elif key == curses.KEY_F5:
                    message = 'Enter загружает выделенный пресет. Затем F5 выполнит запрос.'
                elif key in ('\n', '\r'):
                    lab.load(lab.spec['presets'][menu_index]['id'])
                    menu, focus, position = False, 0, 0
                    lab.panel('workspace')
                    message = 'Пресет загружен. Модель не запускалась.'
            elif key == curses.KEY_F4:
                if pending:
                    lab.save('edited')
                    pending = False
                menu = True
                menu_index = next((i for i,p in enumerate(lab.spec['presets']) if p['id'] == lab.state['preset']), 0)
                lab.panel('presets', lab.spec['presets'][menu_index]['id'])
            elif key == curses.KEY_F5:
                lab.start()
                output_offset = 0
            elif key == curses.KEY_F6:
                lab.cancel()
            elif key == curses.KEY_F7:
                lab.switch_mode()
                focus, position = 0, 0
            elif key == curses.KEY_F8:
                lab.switch_model()
            elif key == curses.KEY_F9:
                lab.assemble()
                position = 0
            elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
                output_offset += 3 if key == curses.KEY_NPAGE else -3
            elif key in ('\t', curses.KEY_BTAB):
                focus = (focus + (-1 if key == curses.KEY_BTAB else 1)) % 4
                position = 0
                selected = focus > 0
            elif not lab.busy:
                if focus and PARAMETERS[focus-1] not in phase['parameters']:
                    message = 'Этот параметр откроется позже.'
                    continue
                current = text if focus == 0 else str(lab.state['parameters'][PARAMETERS[focus-1]])
                updated = current
                if key == curses.KEY_LEFT:
                    selected = False
                    position = max(0,position-1)
                elif key == curses.KEY_RIGHT:
                    selected = False
                    position = min(len(current),position+1)
                elif key in (curses.KEY_HOME, '\x01'):
                    selected = False
                    position = current.rfind('\n',0,position)+1
                elif key in (curses.KEY_END, '\x05'):
                    selected = False
                    end = current.find('\n',position)
                    position = len(current) if end < 0 else end
                elif key in (curses.KEY_UP, curses.KEY_DOWN) and focus == 0:
                    visual = prompt_rows(current, max(1, cols-5))
                    line = max((i for i,(start,_) in enumerate(visual) if start <= position),default=0)
                    target = max(0,min(len(visual)-1,line+(1 if key == curses.KEY_DOWN else -1)))
                    position = visual[target][0]+min(position-visual[line][0],len(visual[target][1]))
                elif key == '\x15':
                    selected = False
                    updated, position = '', 0
                elif key in (curses.KEY_BACKSPACE, '\x7f', '\b') and position:
                    updated = current[:position-1]+current[position:]
                    position -= 1
                elif key == curses.KEY_DC:
                    updated = current[:position]+current[position+1:]
                elif isinstance(key,str) and (key.isprintable() or (focus == 0 and key in ('\n','\r'))):
                    char = '\n' if key == '\r' else key
                    if selected and focus:
                        current, position, selected = '', 0, False
                    updated = current[:position]+char+current[position:]
                    position += 1
                if updated != current and len(updated.encode()) <= (MAX_TEXT if focus == 0 else 32):
                    if focus == 0:
                        lab.state[name] = updated
                    else:
                        lab.state['parameters'][PARAMETERS[focus-1]] = updated
                    lab.state['check'] = None
                    pending, last_edit = True, time.monotonic()
        except (ValueError, RuntimeError, OSError) as error:
            message, lab.error = str(error), str(error)
            lab.state['error'] = message
            lab.save('action_error')
    return 0
