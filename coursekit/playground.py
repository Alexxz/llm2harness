"""F9 playground: independent drafts and bounded, journalled model experiments."""
import curses
import json
import threading
import time
import textwrap

from .classroom import Session, _atomic, _read, _setting, MAX_TEXT
from .classroom_console import terminal_text


def prompt_rows(text, width):
    rows=[]
    start=0
    for line in text.split('\n'):
        parts=textwrap.wrap(line,width=max(1,width),replace_whitespace=False,drop_whitespace=False,expand_tabs=False) or ['']
        offset=start
        for part in parts:
            rows.append((offset,part))
            offset+=len(part)
        start+=len(line)+1
    return rows


class Playground:
    def __init__(self, parent, *, preset=None):
        self.parent = parent
        self.example = preset is not None
        self.path = parent.directory / ('playground-example.json' if self.example else 'playground.json')
        state = parent.snapshot()
        self.draft = {'prompt': state['prompt'], **{k: state['settings'][k] for k in ('max_tokens', 'temperature', 'seed')}, 'output': ''}
        if self.path.exists():
            saved = json.loads(_read(self.path, parent.root, 512 * 1024))
            self.draft.update({k: saved[k] for k in self.draft if k in saved})
        if preset is not None:
            self.draft.update(preset)
            self.draft['output'] = ''
        self.child = None
        self.owner = None
        self.thread = None
        self.error = ''
        self.result = None
        self.started = 0
        self.blocked = False
        self.cursor = 0
        while True:
            events = parent.events(after=self.cursor, limit=100)
            if not events:
                break
            self.cursor = events[-1]['sequence']

    def save(self):
        _atomic(self.path, self.draft, self.parent.root)

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, prompt, parameters):
        if self.busy:
            raise ValueError('Запрос уже выполняется. F6 — остановить.')
        state = self.parent.snapshot()
        if state['gate'] == 'closed' or state['recovery_required'] or 'complete' not in state['capabilities'] or self.blocked:
            raise ValueError('Самостоятельные запросы закрыты преподавателем или недоступны в этом уроке.')
        if state['active_run']:
            raise ValueError('Дождись завершения опыта в основном терминале.')
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode('utf-8')) > MAX_TEXT:
            raise ValueError('Нужен непустой промпт размером до 64 KiB.')
        settings = {}
        for name in ('max_tokens', 'temperature', 'seed'):
            try:
                settings[name] = float(str(parameters[name]).replace(',', '.')) if name == 'temperature' else int(parameters[name])
            except (ValueError, TypeError):
                kind = 'число от 0 до 2 (например, 0.9)' if name == 'temperature' else 'целое число'
                raise ValueError(f'{name}: введи {kind}. F8 выбирает параметр; новый ввод заменяет значение.') from None
        for name, value in settings.items():
            _setting(name, value, {'complete'})
        if self.child is None:
            self.child = Session.create(state['lesson'], root=self.parent.root, endpoint=state['endpoint'], allow_remote=state['allow_remote'], sandbox_image=state['sandbox_image'])
            with self.child._lock('metadata.lock'):
                child_state = self.child._state()
                child_state.update(purpose='playground', parent_session=self.parent.id)
                self.child._save(child_state)
            self.child.control('permit', capabilities=['complete'])
            self.owner = self.child.claim_owner()
            self.owner.__enter__()
        self.child.set_prompt(prompt)
        for key, value in settings.items():
            self.child.set_setting(key, value)
        self.child.control('resume')
        self.draft.update(prompt=prompt, output='', **settings)
        self.save()
        self.result, self.error = None, ''
        self.started = time.monotonic()
        self.parent._record('playground_request', 'student', {'playground_session': self.child.id, 'prompt': prompt, 'settings': settings})

        def work():
            try:
                result = self.child.run('complete')
                self.result = result
                self.draft['output'] = result.get('result', {}).get('text', '') if result['status'] == 'completed' else result.get('error', 'Ответ не получен: ' + result['status'])
                self.parent._record('playground_result', 'student', {'playground_session': self.child.id, 'run_id': result['run_id'], 'prompt': prompt, 'settings': settings, 'status': result['status'], 'result': result.get('result'), 'error': result.get('error')})
            except Exception as error:
                self.error = str(error)
            finally:
                self.save()
        self.thread = threading.Thread(target=work, name='playground-request', daemon=True)
        self.thread.start()

    def tick(self):
        for event in self.parent.events(after=self.cursor, limit=100):
            self.cursor = event['sequence']
            if event['event'] in {'control_requested', 'control_applied'}:
                action = event['data'].get('action')
                if action in {'pause', 'stop', 'close'}:
                    self.blocked = True
                    self.cancel()
                elif action == 'resume':
                    self.blocked = False
        state = self.parent.snapshot()
        if state['gate'] == 'closed' or state['recovery_required'] or 'complete' not in state['capabilities']:
            self.blocked = True
            self.cancel()
        return state['gate'] != 'closed'

    def cancel(self):
        if self.child is not None and self.busy:
            self.child.control('stop')

    def close(self):
        if self.child is not None:
            self.child.control('close')
            if self.thread:
                self.thread.join(12)
            if self.busy:
                raise RuntimeError('Остановка запроса песочницы ещё не подтверждена; проверь журнал.')
            if self.owner:
                self.owner.__exit__(None, None, None)
                self.owner = None
        self.save()


def run_playground(parent):
    import sys
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError('Песочница открывается в учебном окне по F9.')
    if parent.snapshot()['lesson']=='04' and parent._find_lesson('04')[1].get('practice_mode')=='learning-lab-v1':
        from .training_lesson import ensure_server
        ensure_server(parent.root)
    playground = Playground(parent)
    try:
        return curses.wrapper(lambda screen: _screen(screen, playground))
    finally:
        playground.close()
        parent._record('playground_closed', 'student', {})


def _screen(screen, playground):
    screen.timeout(100)
    curses.mousemask(curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED)
    playground.parent._record('playground_opened', 'student', {})
    fields = ['prompt', 'max_tokens', 'temperature', 'seed', 'output']
    values = {k: str(playground.draft[k]) for k in fields if k != 'output'}
    focus, position, offset, selected = 0, len(values['prompt']), 0, False
    message = ('Пример из прошлого прогона. F5 — получить настоящий ответ; Esc — вернуться назад.' if playground.example else 'Редактируй текст и запускай F5 сколько угодно раз. F7 — текст; F8 — параметры.')
    was_busy = False

    def put(y, x, text, style=0):
        rows, cols = screen.getmaxyx()
        if 0 <= y < rows and 0 <= x < cols - 1:
            try:
                screen.addnstr(y, x, terminal_text(text, limit=MAX_TEXT), cols - x - 1, style)
            except curses.error:
                pass

    def frame(top, bottom, title, active=False):
        _, cols = screen.getmaxyx()
        width = cols - 1
        label = (' ' + title + ' ')[:width-2]
        style = curses.A_BOLD if active else 0
        put(top, 0, '┌' + label + '─' * (width-2-len(label)) + '┐', style)
        for row in range(top+1, bottom):
            put(row, 0, '│', style)
            put(row, width-1, '│', style)
        put(bottom, 0, '└' + '─' * (width-2) + '┘', style)

    def persist_fields():
        playground.draft.update({k: values[k] for k in values})
        playground.save()

    while playground.tick():
        busy_now = playground.busy
        if was_busy and not busy_now:
            message = 'Запрос завершён. Меняй текст или параметры и снова нажимай F5.'
        was_busy = busy_now
        rows, cols = screen.getmaxyx()
        if rows < 18 or cols < 48:
            screen.erase()
            put(0, 0, 'Увеличь окно хотя бы до 48×18. Esc — сохранить и выйти.')
        else:
            screen.erase()
            put(0, 0, ('ПЕСОЧНИЦА · ПРИМЕР ОШИБКИ' if playground.example else 'ПЕСОЧНИЦА') + '   F7 — текст · F8 — параметры · Tab / мышь — выбрать поле', curses.A_BOLD)
            ph = max(3, min(9, rows // 3, rows - 13))
            frame(2, 3+ph, 'Вход модели · F7' + (' · ввод' if focus == 0 else ''), focus == 0)
            prompt = values['prompt']
            visual = prompt_rows(prompt, cols-5)
            line_no = max((i for i, (start, _) in enumerate(visual) if start <= position), default=0) if focus == 0 else 0
            column = position-visual[line_no][0] if focus == 0 else 0
            top = max(0, line_no - ph + 1)
            for row, (_, line) in enumerate(visual[top:top + ph]):
                put(3 + row, 2, terminal_text(line,limit=MAX_TEXT)[:cols-5])
            sy = 4 + ph
            labels = {'max_tokens': 'max_tokens (длина)', 'temperature': 'temperature (выбор)', 'seed': 'seed (случайность)'}
            for index, name in enumerate(fields[1:4], 1):
                put(sy + index - 1, 0, f'{labels[name]}: ')
                put(sy + index - 1, len(labels[name])+2, f'[{values[name]}]', curses.A_REVERSE if focus == index else curses.A_BOLD)
                if focus == index:
                    put(sy + index - 1, len(labels[name])+len(values[name])+6, '← новое число заменит значение; ↑↓ изменить')
            oy = sy + 4
            busy = playground.busy
            heading = 'Выход · ответ модели'
            if busy:
                heading += '  ' + '|/-\\'[int(time.monotonic() * 8) % 4] + f'  {int(time.monotonic()-playground.started)} с'
            frame(oy-1, rows-4, heading + (' · ↑↓ / PgUp PgDn' if focus == 4 else ''), focus == 4)
            output = terminal_text(playground.draft['output'], limit=MAX_TEXT)
            wrapped = []
            for line in output.splitlines():
                wrapped.extend(textwrap.wrap(line, width=max(10, cols-5), replace_whitespace=False) or [''])
            height = max(1, rows - oy - 4)
            offset = min(max(0, offset), max(0, len(wrapped) - height))
            for index, line in enumerate(wrapped[offset:offset + height]):
                put(oy + index, 2, line)
            status = playground.error or ('Преподаватель приостановил новые запросы.' if playground.blocked else message)
            put(rows-3, 0, 'Параметры: F8 или клик → введите число. Tab — следующее поле. PgUp/PgDn — ответ.')
            put(rows-2, 0, status)
            put(rows-1, 0, 'F5 — запрос   F6 — остановить   Esc — сохранить и закрыть', curses.A_REVERSE)
            try:
                if focus == 0:
                    curses.curs_set(1)
                    screen.move(3 + line_no - top, min(cols-4, 2+column))
                elif focus < 4:
                    curses.curs_set(1)
                    screen.move(sy + focus - 1, min(cols-2, len(labels[fields[focus]])+3+position))
                else:
                    curses.curs_set(0)
            except curses.error:
                pass
        screen.refresh()
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        name = fields[focus]
        if key in {'\x1b', curses.KEY_F9}:
            persist_fields()
            return 0
        if key in {'\t', curses.KEY_BTAB}:
            focus = (focus + (-1 if key == curses.KEY_BTAB else 1)) % 4
            position = len(values.get(fields[focus], ''))
            selected = focus in (1, 2, 3)
            continue
        if key == curses.KEY_F7:
            focus, position, selected = 0, len(values['prompt']), False
            continue
        if key == curses.KEY_F8:
            focus = focus + 1 if focus in (1, 2) else 1
            position, selected = len(values[fields[focus]]), True
            continue
        if key == curses.KEY_MOUSE and rows >= 18 and cols >= 48:
            try:
                _, mx, my, _, buttons = curses.getmouse()
                if buttons & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED):
                    if 3 <= my < 3 + ph:
                        row = min(len(visual)-1, top+my-3)
                        focus, selected = 0, False
                        position = visual[row][0] + min(len(visual[row][1]), max(0, mx-2))
                    elif sy <= my < sy + 3:
                        focus = my-sy+1
                        position, selected = len(values[fields[focus]]), True
                    elif oy <= my < rows-4:
                        focus, selected = 4, False
            except curses.error:
                pass
            continue
        if key in {curses.KEY_NPAGE, curses.KEY_PPAGE}:
            offset += max(1, rows//3) * (1 if key == curses.KEY_NPAGE else -1)
            continue
        if key == curses.KEY_F5:
            try:
                playground.start(values['prompt'], {k: values[k] for k in fields[1:4]})
                offset = 0
                was_busy = True
                message = 'Запрос выполняется; можно редактировать следующий вход, не меняя текущий запрос.'
            except (ValueError, OSError, RuntimeError) as error:
                message = str(error)
                playground.error = ''
            continue
        if key == curses.KEY_F6:
            playground.cancel()
            message = 'Запрошена остановка.'
            continue
        if focus == 4:
            if key in {curses.KEY_DOWN, curses.KEY_NPAGE}:
                offset += 1 if key == curses.KEY_DOWN else max(1, rows//3)
            elif key in {curses.KEY_UP, curses.KEY_PPAGE}:
                offset -= 1 if key == curses.KEY_UP else max(1, rows//3)
            continue
        text = values[name]
        if focus in (1, 2, 3) and key in {curses.KEY_UP, curses.KEY_DOWN}:
            try:
                step = .1 if name == 'temperature' else 1
                value = float(text.replace(',', '.')) if name == 'temperature' else int(text)
                low, high = {'max_tokens': (1, 4096), 'temperature': (0, 2), 'seed': (0, 2**32-1)}[name]
                value = max(low, min(high, value + step * (1 if key == curses.KEY_UP else -1)))
                values[name] = f'{value:.1f}' if name == 'temperature' else str(int(value))
                position, selected = len(values[name]), True
            except ValueError:
                message = 'Введи корректное число; при переходе в поле новое число заменяет старое.'
        elif key == curses.KEY_LEFT:
            position = max(0, position-1)
        elif key == curses.KEY_RIGHT:
            position = min(len(text), position+1)
        elif key in {curses.KEY_HOME, '\x01'}:
            position = text.rfind('\n', 0, position)+1 if focus == 0 else 0
        elif key in {curses.KEY_END, '\x05'}:
            end = text.find('\n', position)
            position = end if focus == 0 and end >= 0 else len(text)
        elif focus == 0 and key in {curses.KEY_UP, curses.KEY_DOWN}:
            start = text.rfind('\n', 0, position)+1
            col = position-start
            if key == curses.KEY_UP and start:
                prior = text.rfind('\n', 0, start-1)+1
                position = min(prior+col, start-1)
            elif key == curses.KEY_DOWN:
                end = text.find('\n', position)
                if end >= 0:
                    next_end = text.find('\n', end+1)
                    position = min(end+1+col, next_end if next_end >= 0 else len(text))
        elif key in {curses.KEY_BACKSPACE, '\x7f', '\b'}:
            if selected:
                values[name], position, selected = '', 0, False
            elif position:
                values[name] = text[:position-1]+text[position:]
                position -= 1
        elif key == curses.KEY_DC:
            values[name] = text[:position]+text[position+1:]
        elif key == '\x15':
            values[name], position = '', 0
        elif isinstance(key, str) and (key.isprintable() or (focus == 0 and key in {'\n', '\r'})):
            if selected:
                text, position, selected = '', 0, False
            character = '\n' if key == '\r' else key
            candidate = text[:position]+character+text[position:]
            if len(candidate.encode('utf-8')) <= (MAX_TEXT if focus == 0 else 32):
                values[name], position = candidate, position+1
    persist_fields()
    return 0
