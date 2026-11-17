"""Две роли в одной учебной сессии: терминал ученика и команды преподавателя."""

import argparse
import json
import os
from pathlib import Path
import select
import shlex
import sys
import time
import unicodedata

from .registry import ROOT


def terminal_text(value, limit=6000):
    """Данные модели не должны управлять терминалом через ANSI, OSC или bidi."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    safe = "".join(
        f"\\u{ord(character):04x}"
        if unicodedata.category(character) in {"Cc", "Cf"} and character not in {"\n", "\t"}
        else character for character in text
    )
    return safe if len(safe) <= limit else safe[:limit] + "\n[Вывод сокращён; полный результат сохранён в учебной сессии]"


def _say(value, *, label=None):
    text = terminal_text(value)
    if label:
        print(f"\n[{label}]")
        print("\n".join("| " + line for line in text.splitlines()))
    else:
        print(text)
    sys.stdout.flush()


def _event_data(event):
    return event.get("data", event)


def _show_teacher_event(event, *, say=_say):
    if event.get("role") != "teacher":
        return
    data = _event_data(event)
    if event.get("event") == "teacher_turn_done":
        say(data["reply"], label="ПРЕПОДАВАТЕЛЬ")
    elif event.get("event") == "teacher_turn_failed":
        say(data["message"], label="ПРЕПОДАВАТЕЛЬ")
    elif event.get("event") in {"advice", "teacher_advice"}:
        say(data.get("message", data), label="ПРЕПОДАВАТЕЛЬ")
    elif event.get("event") == "control_applied":
        message = {
            "resume": "Запуск опытов разрешён.",
            "pause": "Новые запуски на паузе.",
            "close": "Занятие закрыто; журнал сохранён.",
            "permit": "Разрешения учебной сессии обновлены.",
        }.get(data.get("action"))
        if message:
            say(message, label="СЕССИЯ")
    elif event.get("event") == "control_requested" and data.get("action") == "stop":
        say("Запрошена остановка опыта; ожидается подтверждение.", label="СЕССИЯ")


class ConsoleClosed(Exception):
    pass


class StudentConsole:
    def __init__(self, session, *, input_stream=None, output=None):
        self.session = session
        self.say = output or _say
        self.input = input_stream if input_stream is not None else sys.stdin
        self.cursor = 0
        self.pending = bytearray()
        self.input_ended = False

    def poll(self):
        for event in self.session.events(after=self.cursor, limit=100):
            self.cursor = max(self.cursor, event.get("sequence", 0))
            _show_teacher_event(event, say=self.say)
        if self.session.snapshot()["gate"] == "closed":
            raise ConsoleClosed()

    def line(self, prompt="ученик> "):
        self.poll()
        print(prompt, end="", flush=True)
        try:
            descriptor = self.input.fileno()
        except (AttributeError, ValueError, OSError):
            return self.input.readline(65537)
        while True:
            if b"\n" in self.pending:
                line, remainder = self.pending.split(b"\n", 1)
                self.pending = bytearray(remainder)
                return (line + b"\n").decode("utf-8")
            if self.input_ended:
                line = bytes(self.pending)
                self.pending.clear()
                return line.decode("utf-8")
            if select.select([descriptor], [], [], 0.2)[0]:
                # Не смешивать select с буферизированным TextIO.readline:
                # при вставке строк Python мог бы уже держать следующий ввод,
                # пока select ждёт новых байтов терминала.
                data = os.read(descriptor, 4096)
                if not data:
                    self.input_ended = True
                self.pending.extend(data)
                if len(self.pending) > 65536:
                    self.pending.clear()
                    raise ValueError("Строка ввода превышает 64 KiB")
                continue
            self.poll()

    def multiline(self, description):
        self.say(f"{description}\nЗакончить: отдельная строка ::end. Отмена: ::cancel.")
        lines = []
        size = 0
        while True:
            line = self.line("... ")
            if not line:
                raise ConsoleClosed()
            if line.rstrip("\r\n") == "::cancel":
                return None
            if line.rstrip("\r\n") == "::end":
                return "".join(lines)
            size += len(line.encode("utf-8"))
            if size > 65536:
                raise ValueError("Учебный текст ограничен 64 KiB")
            lines.append(line)

    def run_event(self, event):
        sequence = event.get("sequence", 0)
        # Совет мог попасть в журнал между Enter ученика и запуском worker.
        # Не перепрыгивать через такой совет, получив более новое событие опыта.
        while sequence > self.cursor + 1:
            pending = [item for item in self.session.events(after=self.cursor, limit=100) if item.get("sequence", 0) < sequence]
            if not pending:
                break
            for item in pending:
                self.cursor = max(self.cursor, item.get("sequence", 0))
                _show_teacher_event(item, say=self.say)
        self.cursor = max(self.cursor, event.get("sequence", 0))
        _show_teacher_event(event, say=self.say)
        # Технические фазы worker остаются в журнале; они не являются
        # заданием ученику или ответом модели.
        if event.get("event") == "worker_phase" or _event_data(event).get("kind") == "doctor":
            return
        if event.get("event") in {"run_started", "run_stopping", "run_completed", "run_cancelled", "control_applied"}:
            self.say({"event": event["event"], "data": _event_data(event)}, label="ХОД ОПЫТА")

    def lesson(self):
        try:
            self.say(self.session.lesson_intro(), label="НАЧАЛО УРОКА")
        except FileNotFoundError:
            self.say("Введение INTRO.md отсутствует в материалах этого занятия. Сообщи преподавателю; справка по консоли доступна командой help.", label="МАТЕРИАЛЫ УРОКА")

    def show_prompt(self):
        self.say("Ниже текст, подготовленный для следующего запроса. Команда show только показывает его; запрос к модели будет отдельным действием run.")
        prompt = self.session.snapshot()["prompt"]
        self.say(prompt, label="ТЕКСТ ДЛЯ МОДЕЛИ — ВХОД")
        self.session._record("prompt_shown", "student", {"prompt": prompt, "model_called": False})

    def show_doctor(self, result):
        data = result.get("result")
        if result.get("status") != "completed" or not isinstance(data, dict):
            self.say(result, label="ПРОВЕРКА ПОДГОТОВКИ")
            return
        weights = data.get("local_weights", [])
        self.say("\n".join([
            "Это проверка файлов и программ на компьютере.",
            f"Python: {data.get('python', 'неизвестно')}.",
            "Программа запуска модели: " + ("найдена." if data.get("llama_server") else "не найдена."),
            f"Файлов весов в каталоге курса: {len(weights)}.",
            "Модель использует веса — файлы с её обученными параметрами.",
            "Для настоящего запроса нужны выбранные веса и работающий сервер.",
            "doctor не проверяет доступность сервера и не подтверждает готовность модели.",
            f"Сетевых запросов этой проверки: {data.get('network_requests', 'неизвестно')}.",
            "Полные технические данные сохранены в журнале; их можно посмотреть командой status.",
        ]), label="ПРОВЕРКА ПОДГОТОВКИ")

    def execute(self, line):
        if len(line.encode("utf-8")) > 65536:
            raise ValueError("Команда слишком длинная")
        arguments = shlex.split(line)
        if not arguments:
            return True
        command, args = arguments[0], arguments[1:]
        known = {"exit", "quit", "help", "lesson", "status", "data", "report", "baseline", "check", "compare", "show", "prompt", "edit", "load", "set", "schema", "ask", "read", "write", "copy", "doctor", "run", "agent", "mcp", "bash", "answer"}
        if command in known:
            # Только имя отправленной команды. Валидные параметры и тексты
            # попадают в отдельные семантические события после проверки.
            self.session.record_command(command)
        if command in {"exit", "quit"}:
            self.session.control("pause")
            return False
        if command == "help":
            self.say(HELP, label="СПРАВКА")
        elif command == "lesson" and not args:
            self.lesson()
        elif command == "status":
            state = self.session.snapshot()
            self.say({key: value for key, value in state.items() if key != "prompt"})
        elif command in {"show", "prompt"}:
            self.show_prompt()
        elif command in {"data", "report"} and self.session.snapshot()["lesson"] == "04" and self.session._find_lesson("04")[1].get("practice_mode") == "learning-lab-v1":
            self.say("Этот маршрут заменён наглядным опытом. F8 открывает практикум; F5 выполняет подписанный шаг. Данные прежнего прохождения сохраняются.")
        elif command == "baseline" and self.session.snapshot()["lesson"] == "04" and self.session._find_lesson("04")[1].get("practice_mode") == "learning-lab-v1":
            self.say("В новом уроке не нужна отдельная исходная серия. Открой F8: сначала один ответ, затем пары, обучение и сравнение.")
        elif command in {"data", "report"}:
            from . import training_lesson
            self.say(getattr(training_lesson, command + "_command")(self.session, args), label="УЧЕБНЫЕ ДАННЫЕ" if command == "data" else "ИСХОДНОЕ ИЗМЕРЕНИЕ")
        elif command in {"check", "compare"} and not args:
            from . import structured
            self.say(getattr(structured, command)(self.session), label="ПРОВЕРКА ОТВЕТА" if command == "check" else "СРАВНЕНИЕ ОПЫТОВ")
        elif command == "edit":
            value = self.multiline("Введи новый промпт или задачу")
            if value is not None:
                self.session.set_prompt(value)
                self.say("Текст сохранён. Он попадёт только в следующий явно запущенный опыт.")
        elif command == "load" and len(args) == 1:
            self.session.load_prompt(args[0])
            self.show_prompt()
        elif command == "set" and len(args) >= 2:
            value = " ".join(args[1:])
            if args[0] == "tools":
                value = [name.strip() for name in value.split(",") if name.strip()]
            elif args[0] == "skill":
                if value not in {"on", "off"}:
                    raise ValueError("skill: on либо off")
                value = value == "on"
            else:
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    pass
            self.session.set_setting(args[0], value)
            self.say(self.session.snapshot()["settings"], label="НАСТРОЙКИ СЛЕДУЮЩЕГО ОПЫТА")
        elif command == "schema" and len(args) == 1:
            self.session.set_schema(None if args[0] == "off" else args[0])
            self.say("Схема следующего опыта изменена.")
        elif command == "ask" and args:
            self.say("Вопросы и объяснения пиши в канале преподавателя: в режиме --split это левая область, F2. Здесь команды опытов.", label="СООБЩЕНИЕ ПРЕПОДАВАТЕЛЮ")
        elif command == "read" and len(args) == 1:
            self.say(self.session.read_artifact(args[0]), label="УЧЕБНЫЙ ФАЙЛ")
        elif command == "write" and len(args) == 1:
            value = self.multiline(f"Текст учебного файла {args[0]}")
            if value is not None:
                self.session.write_artifact(args[0], value)
                self.say("Учебный файл сохранён.")
        elif command == "copy" and len(args) == 2:
            self.session.copy_asset(args[0], args[1])
            self.say("Актив текущего занятия скопирован в рабочую папку.")
        elif command in {"doctor", "run", "agent", "mcp", "bash", "baseline"} and not args:
            kind = "complete" if command == "run" else command
            result = self.session.run(kind, on_event=self.run_event)
            if kind == "doctor":
                self.show_doctor(result)
            else:
                self.say(result, label="РЕЗУЛЬТАТ ОПЫТА")
        elif command == "answer" and not args:
            value = self.multiline("Ответ на вопросы учебного агента")
            if value is not None:
                self.say(self.session.run("continue", input_text=value, on_event=self.run_event), label="РЕЗУЛЬТАТ ПРОДОЛЖЕНИЯ")
        else:
            raise ValueError("Здесь вводятся команды опытов; их список — help. Вопросы и ответы преподавателю пиши в канале преподавателя.")
        return True

    def welcome(self):
        self.say("Ты открыл учебную программу курса. Приглашение ученик> принимает её команды. Преподаватель назначается отдельно; его канал указывается до занятия; модель подключается только при отдельном запуске опыта. Открытие консоли не вызывает модель.", label="КАК УСТРОЕНО ЗАНЯТИЕ")
        self.lesson()
        state = self.session.snapshot()
        gate = {"paused": "на паузе: чтение и редактирование доступны; вычислительные опыты разрешит преподаватель", "open": "запуски разрешены преподавателем", "closed": "сессия закрыта"}.get(state["gate"], state["gate"])
        self.say(f"Сессия: {self.session.id}\nСостояние: {gate}.\nПовторить введение: lesson. Справка по командам: help. Технические данные: status.\nРабочие файлы: {self.session.workspace}", label="СЕССИЯ")

    def run(self):
        with self.session.claim_owner():
            self.welcome()
            while True:
                try:
                    line = self.line()
                    if not line or not self.execute(line):
                        break
                except ConsoleClosed:
                    self.say("Учебная консоль закрыта; журнал и файлы сохранены.")
                    break
                except KeyboardInterrupt:
                    self.pending.clear()
                    print()
                    self.say("Ввод отменён. Для выхода введи exit.")
                except (ValueError, OSError, RuntimeError) as error:
                    self.session.record_error(str(error))
                    self.say(str(error), label="ОПЫТ НЕ ВЫПОЛНЕН")
            if self.session.snapshot()["gate"] != "closed":
                self.session.control("pause")
        return 0


HELP = """Сначала ориентирование в занятии:
  lesson                       перечитать цель, объяснение и первое задание
  show                         увидеть текст для модели; запрос не отправляется
  doctor                       проверить программы и файлы без сети и модели
  Вопросы и объяснения пиши в назначенном канале преподавателя.

Работа с текстом и данными:
  show                         показать точный текст следующего запроса
  load baseline | variation    загрузить исходный или изменённый промпт занятия
  data                         урок 04: учебный и проверочный набор без модели
  baseline                     урок 04: шесть настоящих запросов до обучения
  report                       урок 04: исходный отчёт; report 1 — ответ; report freeze — сохранить
  check                        урок 03: проверить последний ответ без вызова модели
  compare                      урок 03: сравнить baseline и variation
  edit                         написать свой текст до строки ::end
  set temperature 0.7          изменить одну настройку
  set max_tokens 256           ограничить длину ответа
  set seed 42                  выбрать seed
  schema schema.json | off    подключить схему из текущего занятия
  read notes.txt               прочитать файл только своей учебной папки
  write notes.txt              записать текст до строки ::end
  copy fixtures/stock.json stock.json  скопировать актив занятия

Явные запуски после разрешения преподавателя:
  doctor                       проверить подготовку без модели и сети
  run                          один запрос текущего текста к ядру
  agent                        новый ограниченный ход агента по текущему тексту
  answer                       ответить ожидающему агенту и продолжить его ход
  mcp                          обнаружить инструменты и прочитать notes.txt
  bash                         выполнить текущий текст только в контейнере

Компоненты агента:
  set tools read_file,write_file    выбрать разрешённые для опыта инструменты
  set max_steps 8                  выбрать предел шагов
  set max_requests 8               выбрать предел запросов
  set skill on | off               подключить учебный GrillMe

Общение и управление:
  status                       увидеть настройки, разрешения и активный опыт
  help                         показать эту справку
  exit                         выйти с сохранением сессии на паузе

Нажатия клавиш не записываются. Журнал содержит отправленный текст, настройки,
явные запуски и их события. Советы преподавателя сами не исполняются и не
добавляются в промпт. Команда bash требует отдельного разрешения и готового образа.
"""


def main(argv=None, *, root=ROOT):
    from .classroom import Session

    parser = argparse.ArgumentParser(description="Совместная учебная работа: терминал ученика и наблюдение преподавателя")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "create"):
        create = commands.add_parser(name, help="Создать занятие" + (" и открыть терминал ученика" if name == "start" else " без подключения терминала"))
        create.add_argument("--lesson", required=True)
        create.add_argument("--endpoint", default=None)
        create.add_argument("--allow-remote", action="store_true")
        create.add_argument("--sandbox-image", default="bash:5.2")
        if name == "start":
            create.add_argument("--split", action="store_true", help="Две области: урок и общение с преподавателем; опыты")
    student = commands.add_parser("student", help="Подключить один терминал ученика к существующей сессии")
    student.add_argument("session")
    student.add_argument("--split", action="store_true", help="Открыть урок и экспериментальную область рядом")
    student.add_argument("--experiment", action="store_true", help="Только экспериментальная область для режима tmux")
    board = commands.add_parser("board", help="Материалы урока и сообщения преподавателю для той же сессии")
    board.add_argument("session")
    board.add_argument("--wait-for-layout", action="store_true", help=argparse.SUPPRESS)
    help_screen = commands.add_parser("help-screen", help="Справка по учебным терминалам")
    help_screen.add_argument("session")
    learning = commands.add_parser("learning-screen", help="Наглядный практикум обучения")
    learning.add_argument("session")
    editor = commands.add_parser("prompt-editor", help="Редактор учебного запроса")
    editor.add_argument("session")
    playground = commands.add_parser("playground", help="Песочница модели")
    playground.add_argument("session")
    result_screen = commands.add_parser("result-screen", help="Читать последний ответ модели")
    result_screen.add_argument("session")
    refresh = commands.add_parser("refresh", help="Обновить плашку помощи и названия открытых терминалов")
    refresh.add_argument("session")
    refresh.add_argument("--redraw", action="store_true", help="Перечитать оформление и введение в терминале преподавателя")
    commands.add_parser("list", help="Показать учебные сессии этого проекта")
    observe = commands.add_parser("observe", help="Снимок состояния и новые события для преподавателя")
    observe.add_argument("session")
    observe.add_argument("--after", type=int, default=0)
    observe.add_argument("--limit", type=int, default=30)
    observe.add_argument("--wait-seconds", type=float, default=0)
    observe.add_argument("--json", action="store_true")
    for name in ("pause", "resume", "stop", "close"):
        control = commands.add_parser(name)
        control.add_argument("session")
        if name == "stop":
            control.add_argument("--run-id")
    advice = commands.add_parser("advise", help="Совет, который появится в учебной консоли")
    advice.add_argument("session")
    advice.add_argument("--message", required=True)
    permit = commands.add_parser("permit", help="Назначить явно разрешённые компоненты занятия")
    permit.add_argument("session")
    permit.add_argument("--capabilities", required=True, help="Например doctor,complete,agent,mcp; bash разрешается отдельно")
    args = parser.parse_args(argv)
    try:
        if args.command in {"create", "start"}:
            if args.command == "start":
                from .preparation import prepare
                prepare(root, args.lesson, endpoint=args.endpoint, allow_remote=args.allow_remote, split=args.split)
            session = Session.create(args.lesson, endpoint=args.endpoint, allow_remote=args.allow_remote, sandbox_image=args.sandbox_image, root=root)
            if args.command == "start":
                if args.split:
                    from .classroom_tmux import run_split
                    return run_split(session)
                return StudentConsole(session).run()
            print(json.dumps(session.snapshot(), ensure_ascii=True, indent=2))
            return 0
        if args.command == "list":
            directory = Path(root) / "results/classroom"
            sessions = []
            if directory.exists():
                for path in sorted(directory.iterdir()):
                    if path.is_symlink() or not path.is_dir():
                        continue
                    try:
                        opened = Session.open(path.name, root=root)
                        if opened._state().get("purpose") in {"playground", "learning_lab"}:
                            continue
                        snapshot = opened.snapshot()
                        sessions.append({key: snapshot.get(key) for key in ("id", "lesson", "gate", "active_run", "owner_pid")})
                    except (ValueError, OSError, RuntimeError):
                        continue
            print(json.dumps(sessions, ensure_ascii=True, indent=2))
            return 0
        session = Session.open(args.session, root=root)
        if args.command == "student":
            if args.split and args.experiment:
                raise ValueError("Режимы split и experiment выбираются отдельно")
            if args.split:
                from .classroom_tmux import run_split
                return run_split(session)
            if args.experiment:
                from .classroom_tmux import ExperimentConsole
                return ExperimentConsole(session).run()
            return StudentConsole(session).run()
        if args.command == "board":
            from .classroom_tmux import run_board
            return run_board(session, wait_for_layout=args.wait_for_layout)
        if args.command == "help-screen":
            from .classroom_tmux import run_help
            return run_help(session)
        if args.command == "learning-screen":
            from .learning_lab import run_lab
            return run_lab(session)
        if args.command == "prompt-editor":
            from .prompt_editor import run_editor
            return run_editor(session)
        if args.command == "playground":
            from .playground import run_playground
            return run_playground(session)
        if args.command == "result-screen":
            from .classroom_tmux import run_result
            return run_result(session)
        if args.command == "refresh":
            from .classroom_tmux import refresh_interface
            result = refresh_interface(session, redraw=args.redraw)
        elif args.command == "advise":
            result = session.advise(args.message)
        elif args.command == "permit":
            result = session.control("permit", capabilities=[item.strip() for item in args.capabilities.split(",") if item.strip()])
        elif args.command in {"pause", "resume", "stop", "close"}:
            result = session.control(args.command, target_run_id=getattr(args, "run_id", None))
        elif args.command == "observe":
            if args.after < 0 or not 1 <= args.limit <= 100 or not 0 <= args.wait_seconds <= 30:
                raise ValueError("after >= 0, limit 1..100, wait-seconds 0..30")
            deadline = time.monotonic() + args.wait_seconds
            while True:
                events = session.events(after=args.after, limit=args.limit)
                snapshot = session.snapshot()
                if events or time.monotonic() >= deadline or snapshot["gate"] == "closed":
                    break
                time.sleep(0.1)
            result = {"session": snapshot, "events": events, "cursor": max([args.after, *[event["sequence"] for event in events]])}
            if not args.json:
                _say({key: snapshot.get(key) for key in ("id", "lesson", "gate", "settings", "capabilities", "active_run", "last_run")}, label="СОСТОЯНИЕ ЗАНЯТИЯ")
                for event in events:
                    _say(event, label=f"СОБЫТИЕ {event['sequence']}")
                _say(f"Следующее чтение: --after {result['cursor']}")
                return 0
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (ValueError, OSError, RuntimeError) as error:
        print(json.dumps({"ok": False, "error": str(error), "error_type": type(error).__name__}, ensure_ascii=True, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
