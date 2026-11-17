#!/usr/bin/env python3
"""Repeat actual lesson-04 training outside student sessions; never tune on the probe."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from coursekit.classroom import Session
from coursekit.learning_lab import ensure_prepared
from coursekit.structured import evaluate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=int, default=10)
    parser.add_argument('--output', type=Path)
    options = parser.parse_args()
    if not 1 <= options.runs <= 100:
        parser.error('--runs must be between 1 and 100')
    ensure_prepared(ROOT)
    source = ROOT / 'lessons/04-training-data'
    spec = json.loads((source / 'workspace.json').read_text())
    schema = json.loads((source / 'core-schema.json').read_text())
    output = options.output or ROOT / 'results/preparation' / ('learning-stability-' + uuid.uuid4().hex)
    output.mkdir(parents=True, exist_ok=False)
    sandbox = output / 'lab-root'
    shutil.copytree(source, sandbox / 'lessons/04-training-data')
    report = {
        'purpose': 'author-training-stability-check', 'not_a_student_session': True,
        'protocol': 'Fresh original model and fresh LoRA for each training seed; fixed inference seed=42 and temperature=0. No tuning or stopping based on probe results.',
        'requested_runs': options.runs, 'training_seeds': list(range(42, 42 + options.runs)),
        'inference_seed': 42, 'temperature': 0,
        'config': spec['training'],
        'source_hashes': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in (source / 'workspace.json', ROOT / 'tools/learning_episode_worker.py')},
        'runs': [], 'complete': False,
    }
    def save():
        target = output / 'report.json'
        temporary = output / 'report.tmp'
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(target)
    save()
    started = time.monotonic()
    for index, seed in enumerate(report['training_seeds'], 1):
        trial = {'index': index, 'training_seed': seed, 'operations': {}, 'ok': False}
        report['runs'].append(trial)
        session = Session.create('04', root=sandbox)
        trial['session'] = session.id
        adapter_id = uuid.uuid4().hex
        try:
            with session.claim_owner():
                session.control('resume')
                session.set_setting('timeout', 180)
                session.set_setting('deadline_seconds', 180)
                def execute(operation, preset='baseline', training_seed=42):
                    config = copy.deepcopy(spec['training'])
                    p = next(p for p in spec['presets'] if p['id'] == preset)
                    config.update(raw_prompt=(source / p['prompt_file']).read_text(),
                                  max_new_tokens=p['parameters']['max_tokens'],
                                  temperature=0, seed=training_seed)
                    payload = {'operation': operation, 'config': config, 'adapter_id': adapter_id}
                    envelope = session.run('learn', input_text=json.dumps(payload, ensure_ascii=False))
                    if envelope['status'] != 'completed':
                        raise RuntimeError(envelope.get('error', envelope['status']))
                    result = envelope['result']
                    item = {'run_id': envelope['run_id'], 'result': result}
                    if operation != 'train':
                        request = spec['training']['new_request' if preset == 'new_request' else 'request']
                        item['check'] = evaluate(result['text'], 'Текущая заявка: ' + request + '\nОтвет:\n', schema)
                        item['success'] = all(item['check'][k] is True for k in ('json_valid', 'schema_valid', 'content_correct'))
                    trial['operations'][operation if preset == 'baseline' else 'new_request'] = item
                    save()
                    return item
                before = execute('before')
                trained = execute('train', training_seed=seed)
                value = trained['result']
                if not value['completed'] or value['parameter_digest_before'] == value['parameter_digest_after'] or value['steps'] != spec['training']['steps'] or value['seed'] != seed:
                    raise RuntimeError('Training provenance or parameter change was not confirmed')
                after = execute('after')
                if any(before['result'][key] != after['result'][key] for key in ('prompt', 'settings', 'base_model', 'base_revision')):
                    raise RuntimeError('Before/after conditions differ')
                execute('after', preset='new_request')
                trial['ok'] = True
        except Exception as error:
            trial['error'] = str(error)
        save()
        after = trial['operations'].get('after', {})
        probe = trial['operations'].get('new_request', {})
        print(f"{index}/{options.runs} seed={seed} finished={trial['ok']} familiar={after.get('success')} new={probe.get('success')}", flush=True)
    report['complete'] = True
    report['seconds'] = round(time.monotonic() - started, 2)
    report['summary'] = {
        'training_completed': sum(t['ok'] for t in report['runs']),
        'familiar_before_success': sum(t['operations'].get('before', {}).get('success', False) for t in report['runs']),
        'familiar_after_success': sum(t['operations'].get('after', {}).get('success', False) for t in report['runs']),
        'new_request_success': sum(t['operations'].get('new_request', {}).get('success', False) for t in report['runs']),
        'unique_adapter_hashes': len({t['operations']['train']['result']['adapter_weights_sha256'] for t in report['runs'] if 'train' in t['operations']}),
        'unique_familiar_outputs': len({t['operations']['after']['result']['text'] for t in report['runs'] if 'after' in t['operations']}),
    }
    seconds = [t['operations']['train']['result']['training_seconds'] for t in report['runs'] if 'train' in t['operations']]
    if seconds:
        report['summary']['training_seconds_mean'] = round(statistics.mean(seconds), 2)
    save()
    print(json.dumps(report['summary'], ensure_ascii=False), flush=True)
    print(output / 'report.json', flush=True)
    return 0 if all(t['ok'] for t in report['runs']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
