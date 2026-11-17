"""Учебная сессия: один терминал ученика, наблюдение и управление преподавателя.

Команды модели никогда не исполняются этим модулем. Каждый опыт получает
неизменяемый запрос и отдельный доверенный worker, которым владеет терминал.
"""

from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid

from .model import ModelClient
from .policy import CONTROL_ACTIONS, TOOL_NAMES, WorkspacePolicy
from .registry import ROOT


WORKER_FILE = Path(__file__).with_name("classroom_worker.py")
CAPABILITIES = frozenset({"doctor", "complete", "agent", "bash", "mcp", "skill"})
RUN_KINDS = frozenset({"doctor", "complete", "agent", "continue", "mcp", "bash", "baseline", "learn"})
MAX_TEXT = 64 * 1024
MAX_EVENT_BYTES = 512 * 1024
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
CONTROL_RESERVE = 2 * 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_RUNS = 128
CANCEL_GRACE_SECONDS = 8.0


class ClassroomError(RuntimeError):
    pass


class ClassroomLimit(ClassroomError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _text(value, *, limit=MAX_TEXT, nonempty=False):
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit or (nonempty and not value.strip()):
        raise ValueError("Нужен текст допустимой длины" + (" и непустого содержания" if nonempty else ""))
    return value


def _plain_path(path, base, *, missing=False):
    """Проверяет путь до чтения: никаких переходов, скрытых частей или ссылок."""
    base, path = Path(base).absolute(), Path(path).absolute()
    if any(part.startswith(".") for part in base.parts if part not in {"/", ""}):
        raise ValueError("Скрытые корни для учебных файлов запрещены")
    if not path.is_relative_to(base):
        raise ValueError("Путь выходит из назначенного каталога")
    relative = path.relative_to(base)
    if any(part.startswith(".") or part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Скрытые пути и переходы запрещены")
    current = base
    if base.resolve() != base:
        raise ValueError("Корень не должен содержать ссылки")
    for part in relative.parts:
        current = current / part
        try:
            for attempt in range(4):
                info = current.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 0:
                    break
            else:
                raise ValueError("Файл меняется во время проверки пути")
        except FileNotFoundError:
            if missing:
                continue
            raise
        if stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
            raise ValueError("Символические и жёсткие ссылки запрещены")
    return path


def _mkdir(path, base):
    path = _plain_path(path, base, missing=True)
    current = Path(base).absolute()
    for part in path.relative_to(current).parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("Назначенный каталог небезопасен")
    return path


def _read(path, base, limit):
    # Atomic replacement can unlink an already opened old inode. Retry the path,
    # never relax the rejection of symlinks or files with multiple hard links.
    for attempt in range(4):
        path = _plain_path(path, base)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1 or info.st_size > limit:
                raise ValueError("Нужен ограниченный обычный файл без ссылок")
            if info.st_nlink == 0:
                continue
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ClassroomLimit("Файл превышает допустимый объём")
        return data
    raise ValueError("Файл меняется во время чтения")


def _atomic(path, value, base):
    path = _plain_path(path, base, missing=True)
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
    if len(payload) > MAX_RESULT_BYTES:
        raise ClassroomLimit("Состояние превышает учебный предел")
    temporary = path.with_name("write-" + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _settings():
    return {"temperature": 0.0, "max_tokens": 256, "seed": 42, "timeout": 60.0,
            "max_steps": 8, "max_requests": 8, "deadline_seconds": 180.0,
            "tools": ["read_file", "write_file"], "skill": False}


def _setting(name, value, capabilities):
    integer_bounds = {"max_tokens": (1, 4096), "seed": (0, 2**32 - 1), "max_steps": (1, 256), "max_requests": (1, 512)}
    number_bounds = {"temperature": (0, 2), "timeout": (0.01, 300), "deadline_seconds": (0.01, 3600)}
    if name in integer_bounds:
        low, high = integer_bounds[name]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name}: целое число {low}..{high}")
    elif name in number_bounds:
        low, high = number_bounds[name]
        if type(value) not in {int, float} or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{name}: конечное число {low}..{high}")
    elif name == "tools":
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value) or set(value) - TOOL_NAMES - CONTROL_ACTIONS:
            raise ValueError("tools: список известных инструментов")
        if "run_bash" in value and "bash" not in capabilities:
            raise ClassroomError("Преподаватель не разрешил bash")
        if "mcp_call" in value and "mcp" not in capabilities:
            raise ClassroomError("Преподаватель не разрешил MCP")
        value = list(dict.fromkeys(value))
    elif name == "skill":
        if type(value) is not bool:
            raise ValueError("skill: логическое значение")
        if value and "skill" not in capabilities:
            raise ClassroomError("Преподаватель не разрешил подключение навыка")
    else:
        raise ValueError("Неизвестная настройка занятия")
    return value


def _run_settings(kind, settings, capabilities):
    # Dormant agent settings must not prevent an offline diagnostic or completion.
    for name, value in settings.items():
        _setting(name, value, CAPABILITIES)
    if kind in {"agent", "continue"}:
        _setting("tools", settings["tools"], capabilities)
        _setting("skill", settings["skill"], capabilities)


class Session:
    def __init__(self, identifier, root):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{5,80}", identifier):
            raise ValueError("Неправильный идентификатор учебной сессии")
        self.root = Path(root).absolute()
        if not self.root.is_dir() or self.root.resolve() != self.root:
            raise ValueError("Нужен существующий корень курса без ссылок")
        self.id = identifier
        self.directory = self.root / "results" / "classroom" / identifier
        self.workspace = self.root / "work" / "classroom" / identifier
        self._owner_fd = None
        self._owner_token = None
        self._owner_pid = None
        self._process = None
        self._run_lock = threading.Lock()

    @classmethod
    def create(cls, lesson_id, *, endpoint=None, allow_remote=False, sandbox_image="bash:5.2", root=ROOT):
        identifier = str(lesson_id).zfill(2)
        if not re.fullmatch(r"0[0-9]|1[0-8]", identifier):
            raise ValueError("Номер занятия должен быть от 00 до 18")
        session = cls(f"lesson-{identifier}-{uuid.uuid4().hex[:12]}", root)
        directory, metadata = session._find_lesson(identifier)
        if endpoint is None:
            endpoint = metadata.get('initial_endpoint', 'http://127.0.0.1:8088')
        ModelClient(endpoint, allow_remote=allow_remote)  # validation only; no request
        if not isinstance(sandbox_image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}", sandbox_image):
            raise ValueError("Неправильное имя подготовленного образа")
        settings = _settings()
        initial = metadata.get("initial_generation", {})
        if not isinstance(initial, dict) or set(initial) - {"temperature", "seed", "max_tokens"}:
            raise ValueError("Начальный профиль урока допускает только temperature, seed и max_tokens")
        for name, value in initial.items():
            settings[name] = _setting(name, value, CAPABILITIES)
        _mkdir(session.directory, session.root)
        _mkdir(session.directory / "runs", session.root)
        WorkspacePolicy(session.workspace)
        prompt = _read(directory / "prompts" / "baseline.txt", directory, MAX_TEXT).decode("utf-8")
        state = {"version": 1, "id": session.id, "lesson": identifier, "lesson_directory": str(directory.relative_to(session.root)),
                 "created_at": _now(), "gate": "paused", "prompt": prompt, "schema": None,
                 "settings": settings, "capabilities": ["doctor"] if identifier == "00" else ["doctor", "complete"], "endpoint": endpoint,
                 "allow_remote": bool(allow_remote), "sandbox_image": sandbox_image,
                 "active_run": None, "last_run": None, "owner_pid": None, "owner_token": None,
                 "control_requests": [], "continuation": None, "run_count": 0, "recovery_required": False}
        _atomic(session.directory / "session.json", state, session.root)
        session._record("session_created", "system", {"lesson": identifier, "gate": "paused"})
        return session

    @classmethod
    def open(cls, session_id, *, root=ROOT):
        session = cls(session_id, root)
        state = session._state()
        if state.get("version") != 1 or state.get("id") != session_id:
            raise ValueError("Неправильное состояние учебной сессии")
        WorkspacePolicy(session.workspace)
        return session

    def _find_lesson(self, identifier):
        matches = []
        lessons = self.root / "lessons"
        _plain_path(lessons, self.root)
        for directory in sorted(lessons.iterdir()):
            if directory.is_symlink() or not directory.is_dir() or directory.name.startswith("."):
                continue
            manifest = directory / "lesson.json"
            if not manifest.exists():
                continue
            metadata = json.loads(_read(manifest, lessons, MAX_TEXT))
            if metadata.get("id") == identifier:
                matches.append((directory, metadata))
        if len(matches) != 1:
            raise ValueError("Не найдено ровно одно занятие с таким номером")
        return matches[0]

    def _state(self):
        return json.loads(_read(self.directory / "session.json", self.root, MAX_RESULT_BYTES))

    @contextmanager
    def _lock(self, name):
        path = _plain_path(self.directory / name, self.root, missing=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
                raise ValueError("Небезопасный файл блокировки")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _save(self, state):
        _atomic(self.directory / "session.json", state, self.root)

    def _record(self, event, role, data):
        record = {"sequence": 0, "timestamp": _now(), "event": event, "role": role, "data": data}
        if len(json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_EVENT_BYTES:
            raise ClassroomLimit("Событие превышает предел журнала; опыт должен быть остановлен")
        journal = self.directory / "events.jsonl"
        with self._lock("journal.lock"):
            previous = _read(journal, self.root, MAX_JOURNAL_BYTES + CONTROL_RESERVE) if journal.exists() else b""
            complete = previous[:previous.rfind(b"\n") + 1]
            lines = complete.splitlines()
            sequence = json.loads(lines[-1])["sequence"] if lines else 0
            record["sequence"] = sequence + 1
            encoded = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
            maximum = MAX_JOURNAL_BYTES + (CONTROL_RESERVE if role in {"system", "teacher"} else 0)
            if len(complete) + len(encoded) > maximum or sequence >= 10000:
                raise ClassroomLimit("Исчерпан ограниченный журнал сессии; создайте новую после проверки")
            fd = os.open(journal, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "r+b") as stream:
                stream.truncate(len(complete))  # recover an interrupted final append
                stream.seek(0, os.SEEK_END)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        return record

    def snapshot(self):
        state = self._state()
        keys = ("id", "lesson", "gate", "prompt", "schema", "settings", "capabilities", "active_run", "last_run", "owner_pid", "endpoint", "allow_remote", "sandbox_image", "recovery_required")
        return {**{key: copy.deepcopy(state[key]) for key in keys}, "directory": str(self.directory), "workspace": str(self.workspace)}

    def events(self, after=0, limit=50):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("after >= 0, limit: 1..100")
        journal = self.directory / "events.jsonl"
        with self._lock("journal.lock"):
            payload = _read(journal, self.root, MAX_JOURNAL_BYTES + CONTROL_RESERVE) if journal.exists() else b""
        payload = payload[:payload.rfind(b"\n") + 1]
        selected = []
        for line in payload.splitlines():
            item = json.loads(line)
            if item["sequence"] > after:
                selected.append(item)
                if len(selected) == limit:
                    break
        return selected

    def advise(self, message):
        return self._record("teacher_advice", "teacher", {"message": _text(message, limit=8192, nonempty=True)})

    def ask(self, message):
        return self._record("student_question", "student", {"message": _text(message, limit=8192, nonempty=True)})

    def record_command(self, command):
        return self._record("student_command", "student", {"command": _text(command, limit=8192, nonempty=True)})

    def record_error(self, message):
        return self._record("student_error", "student", {"message": _text(message, limit=8192, nonempty=True)})

    def control(self, action, *, target_run_id=None, capabilities=None):
        return self._control(action, target_run_id=target_run_id, capabilities=capabilities, role="teacher")

    def _control(self, action, *, target_run_id=None, capabilities=None, role):
        if action not in {"pause", "resume", "stop", "close", "permit"}:
            raise ValueError("Неизвестное действие управления")
        if target_run_id is not None and (not isinstance(target_run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", target_run_id)):
            raise ValueError("Нужен UUID конкретного запуска")
        if action == "permit":
            if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities) or set(capabilities) - CAPABILITIES:
                raise ValueError("Нужен точный список известных возможностей")
        receipt = {"request_id": uuid.uuid4().hex, "action": action, "requested_at": _now(), "target_run_id": target_run_id, "status": "requested"}
        with self._lock("metadata.lock"):
            state = self._state()
            active = state["active_run"]
            if action == "resume":
                if state["gate"] == "closed":
                    raise ClassroomError("Закрытую сессию нельзя возобновить")
                if state["recovery_required"]:
                    raise ClassroomError("Исход прежнего запуска неизвестен; нужна ручная проверка и новая сессия")
                state["gate"] = "open"
            elif action in {"pause", "stop"}:
                if state["gate"] != "closed":
                    state["gate"] = "paused"
            elif action == "close":
                state["gate"] = "closed"
            elif action == "permit":
                state["capabilities"] = sorted(set(capabilities) | {"doctor"})
                receipt["capabilities"] = state["capabilities"]
            if action in {"stop", "close"} and active:
                receipt["target_run_id"] = target_run_id or active["id"]
                if receipt["target_run_id"] == active["id"]:
                    if len(state["control_requests"]) >= 64:
                        raise ClassroomLimit("Слишком много неподтверждённых запросов управления")
                    state["control_requests"].append(copy.deepcopy(receipt))
                else:
                    receipt.update(status="applied", outcome="stale_target")
            else:
                receipt.update(status="applied", outcome="no_active_run" if action in {"stop", "close"} else "gate_or_permissions_updated")
            # Publish the request event before another process can observe its queue item.
            self._record("control_requested", role, {**receipt, "status": "requested"})
            self._save(state)
            if receipt["status"] == "applied":
                self._record("control_applied", role, receipt)
        return receipt

    @contextmanager
    def claim_owner(self):
        if self._owner_fd is not None:
            raise ClassroomError("Терминал уже владеет сессией")
        path = _plain_path(self.directory / "owner.lock", self.root, missing=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(descriptor).st_nlink != 1:
                raise ValueError("Небезопасная блокировка владельца")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ClassroomError("К сессии уже подключён терминал ученика") from error
            token = uuid.uuid4().hex
            with self._lock("metadata.lock"):
                state = self._state()
                if state["active_run"]:
                    stale = state["active_run"]
                    state["last_run"] = {**stale, "status": "outcome_unknown", "cleanup": "cleanup_unconfirmed", "ended_at": _now()}
                    state["active_run"] = None
                    state["recovery_required"] = True
                    if state["gate"] != "closed":
                        state["gate"] = "paused"
                state["owner_pid"], state["owner_token"] = os.getpid(), token
                self._save(state)
            self._owner_fd, self._owner_token, self._owner_pid = descriptor, token, os.getpid()
            self._record("owner_attached", "student", {"owner_pid": os.getpid(), "recovery_required": state["recovery_required"]})
            yield self
        finally:
            if self._process is not None and self._process.poll() is None:
                self._signal_owned(self._process, signal.SIGTERM)
                try:
                    self._process.wait(timeout=CANCEL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    self._signal_owned(self._process, signal.SIGKILL)
                    self._process.wait(timeout=2)
            if self._owner_token is not None:
                with self._lock("metadata.lock"):
                    state = self._state()
                    if state.get("owner_token") == self._owner_token:
                        state["owner_pid"], state["owner_token"] = None, None
                        self._save(state)
                self._owner_fd, self._owner_token, self._owner_pid = None, None, None
            os.close(descriptor)

    def _owned(self, state):
        if self._owner_fd is None or self._owner_pid != os.getpid() or state.get("owner_token") != self._owner_token:
            raise ClassroomError("Запуск возможен только в подключённом терминале владельца")

    def _edit(self, callback):
        with self._lock("metadata.lock"):
            state = self._state()
            if state["gate"] == "closed":
                raise ClassroomError("Сессия закрыта")
            value = callback(state)
            self._save(state)
        return value

    def set_prompt(self, text, source="student"):
        if source not in {"student", "teacher"}:
            raise ValueError("Источник должен быть student или teacher")
        _text(text)
        self._edit(lambda state: state.update(prompt=text))
        return self._record("prompt_changed", source, {"prompt": text, "applies_to": "next_run"})

    def set_setting(self, name, value):
        def change(state):
            state["settings"][name] = _setting(name, value, state["capabilities"])
        self._edit(change)
        return self._record("setting_changed", "student", {"name": name, "value": value, "applies_to": "next_run"})

    def _asset(self, relative):
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Нужен относительный актив текущего занятия")
        state = self._state()
        directory = _plain_path(self.root / state["lesson_directory"], self.root)
        return _read(directory / relative, directory, MAX_TEXT).decode("utf-8")

    def load_prompt(self, experiment_id):
        if experiment_id in {"baseline", "variation"}:
            return self.set_prompt(self._asset(f"prompts/{experiment_id}.txt"))
        state = self._state()
        directory = self.root / state["lesson_directory"]
        metadata = json.loads(_read(directory / "lesson.json", directory, MAX_TEXT))
        matches = [item for item in metadata.get("experiments", []) if item["id"] == experiment_id and "prompt_file" in item]
        if len(matches) != 1:
            raise ValueError("Не найдено одного опыта с промптом")
        return self.set_prompt(self._asset(matches[0]["prompt_file"]))

    def lesson_intro(self):
        """Учебное введение — данные интерфейса, без обращения к модели."""
        source = self._asset("INTRO.md")
        rendered = source.replace("{{PROMPT}}", self.snapshot()["prompt"].rstrip("\n"))
        self._record("lesson_intro_shown", "system", {
            "asset": "INTRO.md",
            "text": rendered,
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "rendered_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        })
        return rendered

    def set_schema(self, relative_asset_or_None):
        if relative_asset_or_None is not None:
            value = json.loads(self._asset(relative_asset_or_None))
            if not isinstance(value, dict):
                raise ValueError("JSON Schema должна быть объектом")
            def check_refs(item):
                if isinstance(item, dict):
                    if "$ref" in item and (not isinstance(item["$ref"], str) or not item["$ref"].startswith("#")):
                        raise ValueError("Внешние ссылки в учебной схеме запрещены")
                    for nested in item.values():
                        check_refs(nested)
                elif isinstance(item, list):
                    for nested in item:
                        check_refs(nested)
            check_refs(value)
        self._edit(lambda state: state.update(schema=relative_asset_or_None))
        return self._record("schema_changed", "student", {"schema": relative_asset_or_None, "applies_to": "next_run"})

    def read_artifact(self, path):
        value = WorkspacePolicy(self.workspace).read_file(path)
        self._record("artifact_read", "student", {"path": path, "bytes": len(value.encode("utf-8"))})
        return value

    def write_artifact(self, path, text):
        _text(text)
        def write(state):
            if state["active_run"]:
                raise ClassroomError("Файлы изменяют между опытами; активный запуск использует свою рабочую папку")
            return WorkspacePolicy(self.workspace).write_file(path, text)
        result = self._edit(write)
        self._record("artifact_written", "student", {**result, "content": text})
        return result

    def copy_asset(self, relative_lesson_asset, dest):
        value = self._asset(relative_lesson_asset)
        result = self.write_artifact(dest, value)
        self._record("asset_copied", "student", {"source": relative_lesson_asset, "destination": dest})
        return result

    @staticmethod
    def _signal_owned(process, sig):
        # Only this live Popen handle is authority. Never take a PID from session.json.
        if process.poll() is None:
            try:
                os.killpg(process.pid, sig)
                return True
            except ProcessLookupError:
                pass
        return False

    def _receipt(self, run_id, outcome):
        with self._lock("metadata.lock"):
            state = self._state()
            selected = [item for item in state["control_requests"] if item["target_run_id"] == run_id]
            state["control_requests"] = [item for item in state["control_requests"] if item["target_run_id"] != run_id]
            self._save(state)
        for item in selected:
            self._record("control_applied", "system", {**item, "status": "applied", "outcome": outcome})

    def _failed_run(self, run_id, kind, process, directory, error):
        """Finalize even observer/journal/spawn failures; never leave an admitted job live."""
        launched = process is not None
        cleanup = "confirmed" if not launched else "cleanup_unconfirmed"
        if launched:
            if process.poll() is None:
                self._signal_owned(process, signal.SIGTERM)
                try:
                    process.wait(timeout=CANCEL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    self._signal_owned(process, signal.SIGKILL)
                    process.wait(timeout=2)
            result_path = directory / "result.json"
            if result_path.exists():
                try:
                    saved = json.loads(_read(result_path, self.root, MAX_RESULT_BYTES))
                    if saved.get("run_id") == run_id and saved.get("cleanup") == "confirmed":
                        cleanup = "confirmed"
                except (ValueError, OSError):
                    pass
        status = "outcome_unknown" if launched else "not_started"
        result = {"run_id": run_id, "kind": kind, "status": status, "cleanup": cleanup,
                  "action_outcome": "outcome_unknown" if launched else "not_started",
                  "error_type": type(error).__name__, "error": str(error)[:4096]}
        with self._lock("metadata.lock"):
            current = self._state()
            if not current["active_run"] or current["active_run"]["id"] != run_id:
                return
            current["active_run"] = None
            current["last_run"] = {"id": run_id, "kind": kind, "status": status, "cleanup": cleanup, "ended_at": _now(), "result": result}
            if current["gate"] != "closed":
                current["gate"] = "paused"
            if launched:
                current["recovery_required"] = True
                current["continuation"] = None
            self._save(current)
        # Audit failure must not undo the already persisted fail-closed finalization.
        try:
            self._receipt(run_id, status)
            self._record("run_failed", "system", result)
        except (ValueError, OSError, RuntimeError):
            pass

    def run(self, kind, *, input_text=None, on_event=None):
        if kind not in RUN_KINDS:
            raise ValueError("Неизвестный вид учебного опыта")
        if not self._run_lock.acquire(blocking=False):
            raise ClassroomError("В этом терминале уже выполняется опыт")
        process = None
        run_id = None
        directory = None
        try:
            with self._lock("metadata.lock"):
                state = self._state()
                self._owned(state)
                if state["active_run"]:
                    raise ClassroomError("В сессии уже выполняется опыт")
                if kind in {"baseline", "learn"} and state["lesson"] != "04":
                    raise ClassroomError("Исходная серия baseline доступна только в уроке 04")
                capability = "complete" if kind in {"baseline", "learn"} else "agent" if kind == "continue" else kind
                if capability not in state["capabilities"] or (kind != "doctor" and state["gate"] != "open"):
                    raise ClassroomError("Запуск закрыт преподавателем или требует отдельного разрешения")
                if state["recovery_required"] and kind != "doctor":
                    raise ClassroomError("Исход старого запуска неизвестен; создайте новую сессию после ручной проверки")
                if state["run_count"] >= MAX_RUNS:
                    raise ClassroomLimit("Исчерпан предел запусков этой сессии")
                effective = copy.deepcopy(state["settings"])
                continuation = state["continuation"] if kind == "continue" else None
                if kind == "continue":
                    if not continuation or continuation["status"] != "waiting_for_user":
                        raise ClassroomError("Нет ожидающего ответа учебного агента")
                    _text(input_text, nonempty=True)
                    effective = copy.deepcopy(continuation["settings"])
                _run_settings(kind, effective, state["capabilities"])
                prompt = state["prompt"] if input_text is None else _text(input_text)
                if kind in {"complete", "agent", "continue", "bash"}:
                    _text(prompt, nonempty=True)
                run_id = uuid.uuid4().hex
                directory = _mkdir(self.directory / "runs" / run_id, self.root)
                schema = json.loads(self._asset(state["schema"])) if kind == "complete" and state["schema"] else None
                skills = []
                if continuation:
                    skills = continuation["skills"]
                elif kind == "agent" and effective["skill"]:
                    skills = [_read(self.root / "skills" / "grill-me.md", self.root / "skills", MAX_TEXT).decode("utf-8")]
                request = {
                    "version": 1, "root": str(self.root), "session_id": self.id, "run_id": run_id,
                    "kind": kind, "prompt": prompt, "settings": effective, "schema": schema,
                    "endpoint": state["endpoint"], "allow_remote": state["allow_remote"],
                    "sandbox_image": state["sandbox_image"], "capabilities": state["capabilities"],
                    "workspace": str(self.workspace), "skills": skills,
                    "agent_state_path": continuation["state_path"] if continuation else str(directory / "agent-state.json"),
                }
                _atomic(directory / "request.json", request, self.root)
                state["active_run"] = {"id": run_id, "kind": kind, "status": "starting", "started_at": _now(), "settings": effective}
                state["run_count"] += 1
                self._save(state)
            start_event = self._record("run_started", "student", {"run_id": run_id, "kind": kind, "prompt": prompt, "settings": effective, "capabilities": request["capabilities"], "schema": state["schema"]})
            cursor = start_event["sequence"] - 1
            if on_event:
                on_event(start_event)
                cursor = start_event["sequence"]
            result_path = directory / "result.json"
            environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
            with open(os.devnull, "rb") as null_input, open(os.devnull, "wb") as null_output:
                process = subprocess.Popen(
                    [sys.executable, "-I", str(WORKER_FILE), "--request", str(directory / "request.json"), "--result", str(result_path)],
                    stdin=null_input, stdout=null_output, stderr=null_output, cwd=WORKER_FILE.parent.parent,
                    env=environment, start_new_session=True,
                )
            self._process = process
            with self._lock("metadata.lock"):
                current = self._state()
                current["active_run"]["status"] = "running"
                current["active_run"]["worker_pid"] = process.pid
                self._save(current)
            cancelling = False
            forced = False
            cancel_deadline = None
            watchdog = time.monotonic() + effective["deadline_seconds"] + CANCEL_GRACE_SECONDS
            while True:
                try:
                    for event in self.events(after=cursor, limit=100):
                        cursor = event["sequence"]
                        if on_event:
                            on_event(event)
                    current = self._state()
                    pending = any(item["target_run_id"] == run_id for item in current["control_requests"])
                    if process.poll() is not None:
                        break
                    if (pending or time.monotonic() >= watchdog) and not cancelling:
                        cancelling = True
                        cancel_deadline = time.monotonic() + CANCEL_GRACE_SECONDS
                        self._record("run_stopping", "system", {"run_id": run_id, "scope": "owned_local_worker", "reason": "control_request" if pending else "watchdog"})
                        self._signal_owned(process, signal.SIGTERM)
                    if cancelling and time.monotonic() >= cancel_deadline:
                        forced = True
                        self._signal_owned(process, signal.SIGKILL)
                        process.wait(timeout=2)
                        break
                    time.sleep(0.05)
                except KeyboardInterrupt:
                    self._control("stop", target_run_id=run_id, role="student")
            process.wait(timeout=2)
            if forced:
                result = {"run_id": run_id, "kind": kind, "status": "outcome_unknown", "cleanup": "cleanup_unconfirmed", "error": "Worker принудительно завершён; исход действий требует проверки"}
            elif result_path.exists():
                result = json.loads(_read(result_path, self.root, MAX_RESULT_BYTES))
                if result.get("run_id") != run_id:
                    raise ClassroomError("Результат принадлежит другому запуску")
            else:
                result = {"run_id": run_id, "kind": kind, "status": "outcome_unknown", "cleanup": "cleanup_unconfirmed", "error": "Worker не оставил подтверждённого результата"}
            if result.get("status") == "outcome_unknown":
                result["action_outcome"] = "outcome_unknown"
                result["cleanup"] = "cleanup_unconfirmed"
            if result.get("cleanup") == "cleanup_unconfirmed":
                result["status"] = "outcome_unknown"
            with self._lock("metadata.lock"):
                current = self._state()
                current["active_run"] = None
                current["last_run"] = {"id": run_id, "kind": kind, "status": result["status"], "cleanup": result.get("cleanup"), "ended_at": _now(), "result": result}
                if result.get("status") == "outcome_unknown" or result.get("cleanup") == "cleanup_unconfirmed" or result.get("action_outcome") == "outcome_unknown":
                    current["recovery_required"] = True
                    if current["gate"] != "closed":
                        current["gate"] = "paused"
                if kind in {"agent", "continue"}:
                    current["continuation"] = {"status": "waiting_for_user", "state_path": request["agent_state_path"], "settings": effective, "skills": request["skills"]} if result["status"] == "waiting_for_user" else None
                self._save(current)
            self._receipt(run_id, "cleanup_unconfirmed" if result.get("cleanup") == "cleanup_unconfirmed" else "stopped_local_worker" if result["status"] == "cancelled" else "run_already_finished")
            final = self._record("run_cancelled" if result["status"] == "cancelled" else "run_completed", "system", {"run_id": run_id, "kind": kind, "status": result["status"], "cleanup": result.get("cleanup")})
            for event in self.events(after=cursor, limit=100):
                if on_event:
                    on_event(event)
            return result
        except BaseException as error:
            if run_id is not None:
                try:
                    self._failed_run(run_id, kind, process, directory, error)
                except Exception:
                    # A filesystem failure can prevent persistence; the next owner
                    # still treats the saved active run as uncertain, never replayable.
                    pass
            raise
        finally:
            if process is not None and process.poll() is None:
                self._signal_owned(process, signal.SIGTERM)
                try:
                    process.wait(timeout=CANCEL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    self._signal_owned(process, signal.SIGKILL)
                    process.wait(timeout=2)
            self._process = None
            self._run_lock.release()
