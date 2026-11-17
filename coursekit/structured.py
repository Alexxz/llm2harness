"""Read-only checks of raw lesson-03 answers. No repair or model requests."""
import json
import re
from .classroom import MAX_TEXT, MAX_RESULT_BYTES, _read

NUMBERS = {'один': 1, 'два': 2, 'одна': 1, 'одну': 1, 'две': 2, 'три': 3, 'четыре': 4, 'пять': 5, 'шесть': 6, 'семь': 7, 'восемь': 8, 'девять': 9, 'десять': 10}


def expected_order(prompt):
    # Only the explicitly marked current order, never examples or arbitrary prose.
    matches = re.findall(r'^Текущая заявка: (.+)\nОтвет:\s*\Z', prompt, re.M)
    if len(matches) != 1:
        return None
    match = re.fullmatch(r'(\d{1,6}|' + '|'.join(NUMBERS) + r') (?:чашк[аиу]|чашек) (чая|кофе)\.?', matches[0].strip().lower())
    if not match:
        return None
    amount = int(match[1]) if match[1].isdigit() else NUMBERS[match[1]]
    if amount < 1:
        return None
    return {'drink': 'tea' if match[2] == 'чая' else 'coffee', 'cups': amount}


def evaluate(text, prompt, schema):
    expected = expected_order(prompt)
    report = {'json_valid': False, 'schema_valid': None, 'content_correct': None, 'expected': expected}
    def reject_constant(value):
        raise ValueError('NaN и Infinity не входят в JSON')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Повторяющееся поле: ' + key)
            result[key] = value
        return result
    try:
        value = json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique)
    except json.JSONDecodeError as error:
        report['explanation'] = f'JSON не читается: строка {error.lineno}, столбец {error.colno}. Проверь кавычки, скобки и лишний текст. Исходный ответ сохранён без исправлений.'
        return report
    except ValueError as error:
        report['explanation'] = 'JSON не принимается: ' + str(error)
        return report
    report['json_valid'] = True
    from jsonschema import Draft202012Validator
    errors = list(Draft202012Validator(schema).iter_errors(value))
    report['schema_valid'] = not errors
    if errors:
        report['explanation'] = 'JSON читается, но нужны ровно drink (tea или coffee) и cups (целое число не меньше 1). Проверь поля и типы. Смысл пока не оцениваем.'
    elif expected is None:
        report['explanation'] = 'Поля и типы подходят. Для этой свободной заявки автоматическая проверка смысла недоступна: сравни значения с текстом вместе с преподавателем. Распознаются заявки вида «Две чашки чая» или «3 чашки кофе».'
    else:
        report['content_correct'] = value == expected
        report['explanation'] = 'Поля и типы подходят. ' + ('Сведения совпадают с заявкой.' if value == expected else 'Сведения не совпадают с заявкой; ожидаемые значения: ' + json.dumps(expected, ensure_ascii=False))
    return report


def _lesson(session):
    if session.snapshot()['lesson'] != '03':
        raise ValueError('check и compare подготовлены для урока 03.')
    directory, _ = session._find_lesson('03')
    return directory, json.loads(_read(directory/'core-schema.json', session.root, MAX_TEXT))


def _run(session, run_id):
    if not re.fullmatch(r'[a-f0-9]{32}', run_id):
        raise ValueError('Неверный номер опыта')
    directory = session.directory/'runs'/run_id
    request = json.loads(_read(directory/'request.json', session.root, MAX_RESULT_BYTES))
    result = json.loads(_read(directory/'result.json', session.root, MAX_RESULT_BYTES))
    if request.get('kind') != 'complete' or result.get('status') != 'completed' or not isinstance(result.get('result', {}).get('text'), str):
        raise ValueError('Нужен завершённый текстовый опыт: сначала run.')
    request['observed_model'] = result['result'].get('metadata', {}).get('server', {}).get('model')
    return request, result['result']['text']


def check(session):
    _, schema = _lesson(session)
    last = session.snapshot()['last_run']
    if not last:
        raise ValueError('Ответа ещё нет. Сначала выполни run.')
    request, text = _run(session, last['id'])
    report = {'run_id': last['id'], 'raw_output': text, **evaluate(text, request['prompt'], schema)}
    session._record('structured_checked', 'student', report)
    return format_check(report) + '\n\nДАЛЬШЕ\n' + next_step(session, request['prompt'])


def next_step(session, checked_prompt):
    state = session.snapshot()
    if state['prompt'] != checked_prompt:
        return 'Следующий вход уже изменён. Проверь его через show; эта проверка относится к прежнему ответу. Не загружай шаблон поверх своего текста.'
    directory, _ = _lesson(session)
    baseline = _read(directory/'prompts/baseline.txt', session.root, MAX_TEXT).decode()
    variation = _read(directory/'prompts/variation.txt', session.root, MAX_TEXT).decode()
    if checked_prompt == baseline:
        if state['gate'] != 'open':
            return 'Следующий опыт добавит один пример к той же заявке. Перейди в F2: преподаватель должен открыть допуск перед run.'
        return 'Добавим один пример, сохранив заявку и настройки. Введи load variation, прочитай добавленный пример, затем run. Ждать преподавателя после load не нужно.'
    if checked_prompt == variation:
        return 'Введи compare: сравнение двух сохранённых опытов выполняется без модели. Затем F4 откроет его в большом окне, Esc закроет.'
    return 'Перейди в F2 и опиши, что изменил в своей заявке и что получил. Преподаватель обсудит наблюдение; эта проверка сама не завершает урок.'


def format_check(report):
    def state(value):
        return {True: 'да', False: 'нет', None: 'не оценивалось'}[value]
    return '\n'.join(['JSON читается: ' + state(report['json_valid']), 'Поля и типы подходят: ' + state(report['schema_valid']), 'Сведения совпадают с заявкой: ' + state(report['content_correct']), report['explanation'], 'Проверка локальная: модель не вызывалась, ответ не исправлялся.'])


def compare(session):
    directory, schema = _lesson(session)
    targets = {name: _read(directory/'prompts'/f'{name}.txt', session.root, MAX_TEXT).decode() for name in ('baseline', 'variation')}
    found, cursor = {}, 0
    while True:
        batch = session.events(after=cursor, limit=100)
        if not batch:
            break
        for event in batch:
            if event['event'] != 'run_completed' or event['data'].get('kind') != 'complete':
                continue
            run_id = event['data']['run_id']
            request, text = _run(session, run_id)
            for name, target in targets.items():
                if request['prompt'] == target:
                    found[name] = {'run_id': run_id, 'prompt': target, 'settings': request['settings'], 'schema': request.get('schema'), 'endpoint': request.get('endpoint'), 'model': request.get('observed_model'), 'output': text, 'check': evaluate(text, target, schema)}
        cursor = batch[-1]['sequence']
    if set(found) != set(targets):
        raise ValueError('Для сравнения нужны оба завершённых опыта: load baseline → run и load variation → run. Их можно выполнять после разрешения преподавателя.')
    comparable = found['baseline']['settings'] == found['variation']['settings'] and found['baseline']['schema'] == found['variation']['schema'] and found['baseline']['endpoint'] == found['variation']['endpoint'] and found['baseline']['model'] == found['variation']['model']
    session._record('structured_compared', 'student', {'comparable': comparable, 'runs': found})
    return render_comparison(found, comparable)


def render_comparison(found, comparable):
    blocks = ['F4 — открыть это сравнение в большом окне.\nСРАВНЕНИЕ: ' + ('настройки совпадают.' if comparable else 'настройки различаются — это не чистое сравнение промптов.')]
    for name, title in [('baseline', 'Без примера'), ('variation', 'С примером')]:
        row = found[name]
        blocks.extend([title, 'Вход:\n' + row['prompt'], 'Настройки: ' + json.dumps(row['settings'], ensure_ascii=False), 'Модель: ' + str(row.get('model')) + '; сервер: ' + str(row.get('endpoint')), 'Полный ответ:\n' + row['output'], format_check(row['check'])])
    blocks.append('ДАЛЬШЕ: F10 — изменить только напиток или количество в строке «Текущая заявка»; F5 сохранить. Затем run и check при открытом допуске. Вопросы и обсуждение — F2.')
    return '\n\n'.join(blocks)
