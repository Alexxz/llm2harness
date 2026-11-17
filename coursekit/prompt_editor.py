"""Edit the actual lesson input, without calling a model or using a clipboard."""
import curses
import json
import os
import re
from .classroom import MAX_TEXT, _read


def history_text(session):
    directory, _ = session._find_lesson(session.snapshot()['lesson'])
    first = _read(directory / 'prompts/first-turn.txt', session.root, MAX_TEXT).decode()
    question = _read(directory / 'prompts/variation.txt', session.root, MAX_TEXT).decode()
    cursor, candidates = 0, []
    while True:
        batch = session.events(after=cursor, limit=100)
        if not batch:
            break
        for event in batch:
            data = event['data']
            if event['event'] == 'model_request' and data.get('request', {}).get('prompt') == first:
                candidates.append(data['run_id'])
        cursor = batch[-1]['sequence']
    for run_id in reversed(candidates):
        if not re.fullmatch(r'[a-f0-9]{32}', run_id):
            continue
        path = session.directory / 'runs' / run_id / 'result.json'
        if not path.exists():
            continue
        result = json.loads(_read(path, session.root, MAX_TEXT * 4))
        if result.get('status') == 'completed' and isinstance(result.get('result', {}).get('text'), str):
            text = first + result['result']['text'] + '\n\n' + question
            if len(text.encode()) > MAX_TEXT:
                raise ValueError('История превышает размер запроса.')
            return text, run_id
    raise ValueError('Сначала выполни load first_turn и run: нужен настоящий ответ модели.')


def run_editor(session):
    session._record('prompt_editor_opened', 'student', {'process_id': os.getpid()})
    try:
        return curses.wrapper(lambda screen: _screen(screen, session))
    finally:
        session._record('prompt_editor_closed', 'student', {})


def _screen(screen, session):
    history_mode = session.snapshot()['lesson'] == '02'
    text = session.snapshot()['prompt']
    position, top = len(text), 0
    message = 'Это вход следующего учебного опыта. Сохранение не вызывает модель.'
    screen.timeout(100)
    def put(y, value, style=0):
        rows, cols = screen.getmaxyx()
        try:
            screen.addnstr(y, 0, value, max(1, cols-1), style)
        except curses.error:
            pass
    while True:
        rows, cols = screen.getmaxyx()
        height = max(1, rows-6)
        lines = text.split('\n')
        row = text[:position].count('\n')
        column = len(text[:position].rsplit('\n', 1)[-1])
        top = min(top, row)
        top = max(top, row-height+1)
        left = max(0, column-cols+3)
        screen.erase()
        put(0, 'УЧЕБНЫЙ ЗАПРОС · редактируйте текст ниже', curses.A_BOLD)
        put(1, 'F6 — собрать историю из ответа · F8 — оставить только вопрос' if history_mode else 'Редактируется текущий вход урока. F5 сохраняет, модель запускается отдельно.')
        for index, line in enumerate(lines[top:top+height]):
            # Display control characters as text; keep the underlying prompt unchanged.
            from .classroom_console import terminal_text
            put(3+index, terminal_text(line[left:]))
        put(rows-3, message)
        put(rows-2, 'Стрелки · Home/End · Enter · Backspace/Delete · Ctrl+u очистить')
        put(rows-1, 'F5 — сохранить и закрыть   Esc — отменить', curses.A_REVERSE)
        try:
            screen.move(min(rows-4, 3+row-top), min(cols-2, column-left))
            key = screen.get_wch()
        except curses.error:
            continue
        if key == '\x1b':
            return 0
        if key == curses.KEY_F5:
            try:
                session.set_prompt(text)
                session._record('prompt_editor_saved', 'student', {'model_called': False})
                return 0
            except (ValueError, RuntimeError) as error:
                message = str(error)
        elif key in (curses.KEY_F6, curses.KEY_F8):
            if not history_mode:
                message = 'Сборка истории относится к уроку 02; здесь редактируй текст обычными клавишами.'
                continue
            try:
                if key == curses.KEY_F6:
                    text, run_id = history_text(session)
                    message = 'Собраны первый вход + полный ответ + вопрос. Проверь текст. F5 сохранит.'
                    session._record('history_assembled', 'student', {'source_run_id': run_id, 'model_called': False})
                else:
                    directory, _ = session._find_lesson(session.snapshot()['lesson'])
                    text = _read(directory/'prompts/variation.txt', session.root, MAX_TEXT).decode()
                    message = 'В черновике оставлен только вопрос. F5 сохранит.'
                position = len(text)
            except (ValueError, OSError) as error:
                message = str(error)
        elif key == curses.KEY_LEFT:
            position = max(0, position-1)
        elif key == curses.KEY_RIGHT:
            position = min(len(text), position+1)
        elif key in (curses.KEY_HOME, '\x01'):
            position = text.rfind('\n', 0, position)+1
        elif key in (curses.KEY_END, '\x05'):
            end = text.find('\n', position)
            position = end if end >= 0 else len(text)
        elif key in (curses.KEY_UP, curses.KEY_DOWN):
            start = text.rfind('\n', 0, position)+1
            if key == curses.KEY_UP and start:
                previous = text.rfind('\n', 0, start-1)+1
                position = min(previous+column, start-1)
            elif key == curses.KEY_DOWN:
                end = text.find('\n', position)
                if end >= 0:
                    next_end = text.find('\n', end+1)
                    position = min(end+1+column, next_end if next_end >= 0 else len(text))
        elif key in (curses.KEY_BACKSPACE, '\x7f', '\b') and position:
            text = text[:position-1]+text[position:]
            position -= 1
        elif key == curses.KEY_DC:
            text = text[:position]+text[position+1:]
        elif key == '\x15':
            text, position = '', 0
        elif isinstance(key, str) and (key.isprintable() or key in ('\n', '\r', '\t')):
            value = '\n' if key == '\r' else key
            candidate = text[:position]+value+text[position:]
            if len(candidate.encode()) <= MAX_TEXT:
                text, position = candidate, position+len(value)
