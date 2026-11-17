"""Keep authored explanation/action before observation and assessment."""
from .workbench import definition, phase_index


def _matches(event, requirement):
    if event['event'] != 'workspace_state' or event['data'].get('action') != requirement['action']:
        return False
    state = event['data']['state']
    if requirement['action'] == 'completed' and (state.get('error') or state.get('result') is None):
        return False
    for key in ('preset', 'mode'):
        if key in requirement and state.get(key) != requirement[key]:
            return False
    if 'model' in requirement:
        model = state.get('model')
        if requirement['model'] == 'original' and model != 'original':
            return False
        if requirement['model'] == 'adapted' and model in (None, 'original'):
            return False
    for key, value in requirement.get('parameters', {}).items():
        actual = state.get('last_input', {}).get('parameters', {}).get(key)
        try:
            if float(actual) != float(value):
                return False
        except (TypeError, ValueError):
            return False
    return True


def progress(session, events=None):
    spec = definition(session)
    index = phase_index(session)
    phase = spec['phases'][index]
    if not all(key in phase for key in ('explanation','action','observe','question','evidence')):
        return None
    if events is None:
        events, cursor = [], 0
        while True:
            batch = session.events(after=cursor, limit=100)
            if not batch:
                break
            events.extend(batch)
            cursor = batch[-1]['sequence']
    turns = [e for e in events if e['event']=='teacher_turn_done' and e['data'].get('phase_index')==index]
    intro = next((e for e in reversed(turns) if e['data'].get('source')=='workspace_explanation'), None)
    question = next((e for e in reversed(turns) if e['data'].get('source')=='workspace_question'), None)
    observed = [e for e in events if intro and e['sequence']>intro['sequence']]
    remaining = [r for r in phase['evidence'] if sum(_matches(e,r) for e in observed)<r.get('count',1)]
    # Navigation/help/preset sequences must happen in order, after the explanation.
    ordered = True
    cursor = intro['sequence'] if intro else 0
    if phase['evidence'] and (phase.get('ordered_evidence') or all(r.get('count',1)==1 and r['action']!='completed' for r in phase['evidence'])):
        for requirement in phase['evidence']:
            matching = next((e for e in observed if e['sequence']>cursor and _matches(e,requirement)), None)
            if matching is None:
                ordered = False
                break
            cursor = matching['sequence']
    answered = bool(question and any(e['event']=='student_question' and e['sequence']>question['sequence'] for e in events))
    return {'phase_index':index,'phase':phase,'introduced':bool(intro),
            'experience_completed':bool(intro) and not remaining and ordered,
            'question_asked':bool(question),'student_replied':answered,
            'ready_to_assess':bool(question) and answered,
            'remaining_evidence':remaining,'question':phase['question']}


def render_observation(flow, events):
    """Show immutable model inputs/results before asking about a cleared screen."""
    if not flow['phase'].get('show_runs'):
        return flow['phase']['observe']
    import json
    import re
    index=flow['phase_index']
    intro=next((e for e in reversed(events) if e['event']=='teacher_turn_done' and e['data'].get('source')=='workspace_explanation' and e['data'].get('phase_index')==index),None)
    boundary=intro['sequence'] if intro else 0
    completed=[e for e in events if _matches(e,{'action':'completed'}) and isinstance(e['data']['state'].get('result'),dict) and isinstance(e['data']['state']['result'].get('text'),str)]
    selected=[]
    for reference in flow['phase'].get('reference_runs',[]):
        candidates=[e for e in completed if e['sequence']<boundary and _matches(e,{'action':'completed',**reference})]
        if not candidates:
            candidates=[e for e in completed if _matches(e,{'action':'completed',**reference})]
        if candidates:selected.append(candidates[-1])
    selected.extend(e for e in completed if e['sequence']>boundary)
    selected=list({e['sequence']:e for e in selected}.values())
    if not selected:return flow['phase']['observe']
    def block(text):
        size=max([len(m[0])+1 for m in re.finditer(r'`{3,}',text)]+[3])
        fence='`'*size
        return fence+'text\n'+text+'\n'+fence
    rows=[flow['phase']['observe'],'Ниже — сохранённые запуски. Очистка рабочего поля не удаляет эти результаты.']
    inputs=[e['data']['state'].get('last_input') or {} for e in selected]
    same_input=bool(inputs[0].get('prompt')) and all(item.get('prompt')==inputs[0]['prompt'] for item in inputs)
    if same_input:rows.extend(['**Вход модели — одинаковый для этих запусков**',block(inputs[0]['prompt'])])
    for number,(event,conditions) in enumerate(zip(selected,inputs),1):
        state=event['data']['state']
        rows.append('**Запуск '+str(number)+'**')
        parameters=conditions.get('parameters',state['parameters'])
        model=conditions.get('model',state.get('model'))
        rows.append(('Модель: исходная.' if model=='original' else 'Модель: с обученной добавкой.')+' Параметры: '+', '.join('`'+key+'='+str(parameters[key])+'`' for key in ('max_tokens','temperature','seed'))+'.')
        if not same_input:rows.extend(['**Вход модели**',block(conditions.get('prompt',state['prompt']))])
        rows.extend(['**Выход модели**',block(state['result']['text'])])
        check=state.get('check')
        if check:
            from .structured import explain_checks
            rows.append(explain_checks(check))
    return '\n\n'.join(rows)
