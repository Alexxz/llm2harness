"""Prepare classroom dependencies before creating a learner session."""
import errno
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import socket
import subprocess
import time
from types import SimpleNamespace
import urllib.error
import urllib.request
import uuid

from .model import ModelClient


def props(endpoint):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise ValueError("Перенаправления сервера модели запрещены")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(endpoint.rstrip('/') + '/props', timeout=3) as response:
        return json.loads(response.read(65536))


def available(port):
    with socket.socket() as sock:
        try:
            sock.bind(('127.0.0.1', port))
            return True
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                return False
            raise


def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def ensure_base(root, endpoint, directory, *, timeout=120):
    profiles = json.loads((root / 'resources/models.json').read_text())
    profile = profiles['base']
    weights = root / 'models' / profile['local_name']
    process = None
    if endpoint.rstrip('/') == 'http://127.0.0.1:8088' and available(8088):
        binary = root / 'runtime/llama-b12380/llama-server'
        if not binary.is_file():
            for command in ('git', 'cmake', 'c++'):
                if not shutil.which(command):
                    raise ValueError('Для подготовки llama.cpp установите системный пакет: ' + command)
            (root / 'work').mkdir(exist_ok=True)
            source = root / 'work/llama-b12380-source'
            if not source.exists():
                subprocess.run(['git', 'clone', '--depth', '1', '--branch', 'b12380', 'https://github.com/ggml-org/llama.cpp.git', str(source)], check=True, timeout=300)
            revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True, timeout=10).strip()
            if not revision.startswith('e3483e40b'):
                raise ValueError('Исходники llama.cpp не соответствуют b12380; каталог сохранён.')
            build = root / 'work/llama-b12380-build'
            subprocess.run(['cmake', '-S', str(source), '-B', str(build), '-DGGML_CUDA=OFF', '-DLLAMA_CURL=OFF', '-DCMAKE_BUILD_TYPE=Release'], check=True, timeout=120)
            subprocess.run(['cmake', '--build', str(build), '--target', 'llama-server', '-j', '2'], check=True, timeout=1800)
            # Keep shared libraries beside the executable.
            shutil.copytree(build / 'bin', binary.parent, dirs_exist_ok=True)
        if not weights.is_file():
            print('Загрузка публичных весов base (около 1 GB)…', flush=True)
            subprocess.run(['/usr/bin/python3', str(root / 'tools/prepare_model.py'), '--variant', 'base', '--download'], check=True, timeout=1800)
        with weights.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if weights.stat().st_size != profile['bytes'] or digest != profile['sha256']:
            raise ValueError('Веса base не соответствуют профилю; существующий файл сохранён.')
        print('Запуск локальной модели base; ожидание загрузки…', flush=True)
        with (directory / 'model-server.log').open('xb') as log:
            process = subprocess.Popen([str(binary), '--model', str(weights), '--host', '127.0.0.1', '--port', '8088', '--ctx-size', '4096', '--threads', '4', '--device', 'none'], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        try:
            (directory / 'model-server.json').write_text(json.dumps({'pid': process.pid, 'endpoint': endpoint}))
        except BaseException:
            stop(process)
            raise
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                actual = props(endpoint)
                break
            except (OSError, urllib.error.URLError):
                if time.monotonic() >= deadline or (process is not None and process.poll() is not None):
                    raise ValueError('Сервер модели не готов. См. журнал подготовки: ' + str(directory)) from None
                time.sleep(.5)
        actual_model = actual.get('model_path', actual.get('default_generation_settings', {}).get('model', ''))
        if Path(actual_model).name != profile['local_name']:
            raise ValueError('На endpoint работает другой или неопознанный профиль. Сервер не остановлен и модель не заменена.')
        result = ModelClient(endpoint, allow_remote=True, timeout=30).generate('Translate English to Russian.\ncat =', max_tokens=8, temperature=0, seed=42)
        (directory / 'model-check.json').write_text(json.dumps({'profile': 'base', 'prompt': 'Translate English to Russian.\ncat =', 'props': actual, 'result': result}, ensure_ascii=False, indent=2))
    except BaseException:
        stop(process)
        raise
    return process


def check_teacher(root):
    from .teacher import teacher_backend
    backend = teacher_backend(SimpleNamespace(root=root))
    backend.instruction = 'Проверка подключения до занятия. Не начинай урок. Верни reply="Подключение готово", note="preflight", control="none".'
    backend.context = lambda events: backend.instruction + '\n{}'
    try:
        answer = backend.answer([])
        if answer['control'] != 'none':
            raise ValueError('Проверка преподавателя вернула учебное управление')
    finally:
        backend.cancel_process()


def prepare(root, lesson, *, endpoint=None, allow_remote=False, split=False):
    root = Path(root)
    identifier = str(lesson).zfill(2)
    matches = list((root / 'lessons').glob(identifier + '-*/lesson.json'))
    if len(matches) != 1:
        raise ValueError('Не найден урок ' + identifier)
    metadata = json.loads(matches[0].read_text())
    endpoint = endpoint or metadata.get('initial_endpoint', 'http://127.0.0.1:8088')
    ModelClient(endpoint, allow_remote=allow_remote)
    if identifier not in {'00', '01', '02', '03', '04'}:
        return
    directory = root / 'results/preparation' / (identifier + '-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    process = None
    report = {'lesson': identifier, 'endpoint': endpoint, 'ok': False}
    try:
        if split and not shutil.which('tmux'):
            raise ValueError('Для двух окон нужен tmux: установите пакет tmux средствами вашей ОС.')
        print('Проверка подключения преподавателя (без начала урока)…', flush=True)
        report['stage'] = 'teacher'
        check_teacher(root)
        report['teacher'] = 'ready'
        report['stage'] = 'model'
        with (directory.parent / 'prepare.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if identifier in {'01', '02', '03'}:
                process = ensure_base(root, endpoint, directory)
            elif identifier == '04':
                if endpoint.rstrip('/') != 'http://127.0.0.1:8089':
                    raise ValueError('Урок 04 использует фиксированный локальный профиль 8089.')
                from .learning_lab import ensure_prepared
                print('Проверка изолированной среды обучения и публичных весов…', flush=True)
                subprocess.run(['/usr/bin/python3', str(root / 'tools/prepare_training.py')], check=True, timeout=3900)
                ensure_prepared(root)
                # The actual offline load and generation are checked without training.
                from .training_lesson import ensure_server, profile
                ensure_server(root)
                report['model_profile'] = profile()
                report['model_check'] = ModelClient(endpoint, timeout=60).generate('Hello', max_tokens=8)
        report['ok'] = True
        report['stage'] = 'ready'
        print('Окружение готово. Отчёт: ' + str(directory), flush=True)
    except BaseException as error:
        stop(process)
        report['error_type'] = type(error).__name__
        # Provider diagnostics and credentials must not enter preparation reports.
        if isinstance(error, subprocess.SubprocessError):
            raise ValueError('Не удалось подготовить зависимости: ' + type(error).__name__ + '. Повторите запуск после устранения ошибки.') from None
        raise
    finally:
        (directory / 'preflight.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
