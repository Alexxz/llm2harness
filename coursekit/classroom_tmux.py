"""Два обычных Python терминала в собственном, временном сервере tmux."""

from contextlib import contextmanager
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

from .classroom import MAX_TEXT, _read
from .classroom_console import ConsoleClosed, StudentConsole, _say, _show_teacher_event, terminal_text


TEACHER_LABELS = frozenset({
    "НАЧАЛО УРОКА", "КАК УСТРОЕНО ЗАНЯТИЕ", "СЕССИЯ", "ПРЕПОДАВАТЕЛЬ",
    "УПРАВЛЕНИЕ ПРЕПОДАВАТЕЛЯ", "СПРАВКА", "СООБЩЕНИЕ ПРЕПОДАВАТЕЛЮ", "МАТЕРИАЛЫ УРОКА",
})
KEYS = "F2 — преподаватель. F3 — терминал ученика. Ctrl+h — помощь. Вопросы и ответы — слева, в F2."
CHAT_NOTE = "Вопросы и объяснения пиши слева, в F2. Преподаватель видит журнал опытов справа."
CHILD_CODE = "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from coursekit.classroom_console import main; modes = {'board': ['board', sys.argv[3], '--wait-for-layout'], 'experiment': ['student', sys.argv[3], '--experiment'], 'help': ['help-screen', sys.argv[3]], 'result': ['result-screen', sys.argv[3]], 'learning': ['learning-screen', sys.argv[3]], 'editor': ['prompt-editor', sys.argv[3]], 'playground': ['playground', sys.argv[3]]}; raise SystemExit(main(modes[sys.argv[4]], root=Path(sys.argv[2])))"

HELP_TEXT = """Помощь по учебному терминалу

Терминал преподавателя (УРОК)
Здесь пиши вопросы и ответы преподавателю обычным текстом.

Терминал ученика (ЭКСПЕРИМЕНТ)
Вводи команды учебной программы: show, doctor, edit.

Ctrl+h                 открыть эту помощь из любого терминала
F2                     перейти к материалам преподавателя
F4                     читать последний ответ или сравнение в большом окне
F8                     урок 04: наглядный практикум настоящего обучения
F10                    редактор учебного запроса; F5 сохраняет, Esc отменяет
В уроке 02 внутри F10   F6 собирает историю, F8 оставляет только вопрос
F9                     песочница: промпт, параметры и ответы
F3                     перейти в терминал ученика
Колесо мыши            прокрутить текст
/lesson               перечитать урок в терминале преподавателя
/basics               подробная памятка там же
/exit                 завершить занятие там же

F2 и F3 нажимаются один раз, без предварительных сочетаний.
Разговариваем слева, в F2. Специального слова для
продолжения нет. Ученик сообщает наблюдения преподавателю.
"""


def board_text(value, *, columns=80, markdown=False, limit=6000):
    """Перенос прозы по словам; управляющие символы остаются видимыми данными."""
    width = max(12, columns - 4)
    lines = []
    fenced = False
    for line in terminal_text(value, limit=limit).expandtabs(4).splitlines():
        if markdown and line.strip().startswith("```"):
            fenced = not fenced
            continue
        if markdown and not fenced:
            line = re.sub(r"^#{1,6}\s+", "", line)
            line = re.sub(r"`([^`]+)`", r"\1", line)
        if not line.strip():
            lines.append("")
            continue
        indent = len(line) - len(line.lstrip())
        prefix = " " * min(indent, 8)
        content = line.lstrip()
        continuation = prefix
        marker = re.match(r"(?:[-*]|\d+[.)])\s+", content)
        if marker and not fenced:
            continuation += " " * len(marker[0])
        lines.extend(textwrap.wrap(
            content, width=width, initial_indent=prefix,
            subsequent_indent=continuation, break_long_words=False,
            break_on_hyphens=False, replace_whitespace=not fenced,
        ))
    return "\n".join("  " + line if line else "" for line in lines)


def board_say(value, *, label=None, columns=None):
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.text import Text
    from rich.theme import Theme

    try:
        measured = os.get_terminal_size(sys.stdout.fileno()).columns
    except (AttributeError, OSError, ValueError):
        measured = 80
    columns = min(measured, columns) if columns is not None else measured
    console = Console(
        file=sys.stdout, width=max(12, columns),
        highlight=False, markup=False, emoji=False,
        theme=Theme({
            "markdown.h1": "bold bright_cyan", "markdown.h1.border": "cyan",
            "markdown.h2": "bold bright_cyan", "markdown.h3": "bold yellow",
            "markdown.strong": "bold yellow", "markdown.code": "bold bright_cyan",
            "markdown.item.bullet": "bright_cyan",
        }),
    )
    # Цвета добавляет только renderer после экранирования данных.
    # Markdown-ссылки остаются текстом, без управляющих OSC-ссылок.
    safe = terminal_text(value)
    if label == "ПРЕПОДАВАТЕЛЬ":
        from .teaching_text import teacher_markdown
        safe = teacher_markdown(safe)
    prose = label in {"УРОК", "СПРАВКА", "ОСНОВЫ ТЕРМИНАЛА", "ПРЕПОДАВАТЕЛЬ"}
    content = Markdown(safe, hyperlinks=False, code_theme="monokai", justify="left") if prose else Text(safe)
    if label == "ПРЕПОДАВАТЕЛЬ":
        content = Panel(content, title="Преподаватель", border_style="bright_cyan", padding=(0, 0))
    elif label == "ТВОЯ РЕПЛИКА":
        content = Panel(content, title="Ты", border_style="green", padding=(0, 0))
    elif label == "СООБЩЕНИЕ НЕ ПРИНЯТО":
        content = Panel(content, title="Проверь ввод", border_style="red", padding=(0, 0))
    elif label == "ТЕКСТ ДЛЯ МОДЕЛИ — ВХОД":
        content = Panel(content, title="Вход для модели", border_style="yellow", padding=(0, 0))
    elif label == "ОТВЕТ МОДЕЛИ":
        content = Panel(content, title="Ответ модели", border_style="green", padding=(0, 0))
    elif label and not (label == "УРОК" and safe.lstrip().startswith("#")):
        console.print(Text(label, style="bold cyan"))
    console.print(content)


def child_argv(session, mode):
    """Несколько shell-command аргументов tmux 3.4 означают прямой exec."""
    if mode not in {"board", "experiment", "help", "result", "playground", "editor", "learning"}:
        raise ValueError("Неизвестная область занятия")
    return [sys.executable, "-I", "-c", CHILD_CODE, str(Path(__file__).resolve().parents[1]), str(session.root), session.id, mode]


class ExperimentConsole(StudentConsole):
    def __init__(self, session, *, input_stream=None):
        super().__init__(session, input_stream=input_stream, output=self.route_output)

    def welcome(self):
        self.say("Это терминал ученика: здесь вводятся команды опытов. F2 — материалы преподавателя, F3 — сюда, Ctrl+h — помощь. Вопросы преподавателю пиши слева, в F2.", label="ТЕРМИНАЛ УЧЕНИКА")

    def route_output(self, value, *, label=None):
        if label == "СООБЩЕНИЕ ПРЕПОДАВАТЕЛЮ":
            _say(value, label="ВОПРОСЫ — СЛЕВА, F2")
        elif label in {"СПРАВКА", "МАТЕРИАЛЫ УРОКА"}:
            self.session._record("lesson_help_shown", "system", {"text": value})
        elif label == "ТЕКСТ ДЛЯ МОДЕЛИ — ВХОД":
            board_say(value, label=label)
        elif label == "ХОД ОПЫТА" and isinstance(value, dict):
            event = value.get("event")
            message = {"run_started": "Запрос отправлен. Модель создаёт ответ…", "run_stopping": "Останавливаем опыт…", "run_cancelled": "Опыт остановлен."}.get(event)
            if event == "evaluation_progress":
                data = value.get("data", {})
                message = f"Исходная серия: {data.get('completed')} из {data.get('total')} запросов завершено."
            if message:
                board_say(message)
        elif label == "РЕЗУЛЬТАТ ОПЫТА" and isinstance(value, dict) and value.get("kind") == "baseline" and value.get("status") == "completed":
            from .training_lesson import report_command
            board_say(report_command(self.session, []), label="ИСХОДНОЕ ИЗМЕРЕНИЕ")
        elif label == "РЕЗУЛЬТАТ ОПЫТА" and isinstance(value, dict) and value.get("kind") == "complete":
            if value.get("status") == "completed":
                result = value.get("result", {})
                board_say(result.get("text", ""), label="ОТВЕТ МОДЕЛИ")
                seconds = result.get("metadata", {}).get("elapsed_seconds")
                timing = f" · {seconds:.1f} с" if isinstance(seconds, (int, float)) else ""
                board_say("Запрос завершён" + timing + ". F4 — читать ответ в большом окне.")
            else:
                board_say(value.get("error", "Ответ не получен. Статус: " + str(value.get("status"))), label="ОШИБКА ОПЫТА")
        elif label == "НАСТРОЙКИ СЛЕДУЮЩЕГО ОПЫТА" and isinstance(value, dict):
            board_say(f"Температура: {value.get('temperature')} · seed: {value.get('seed')} · лимит: {value.get('max_tokens')}")
        elif label not in TEACHER_LABELS:
            _say(value, label=label)


def _tail(session):
    cursor = 0
    while True:
        events = session.events(after=cursor, limit=100)
        if not events:
            return cursor
        cursor = events[-1]["sequence"]


class LessonBoard(StudentConsole):
    """Чтение событий и реплики ученика без владения исполнителем."""

    def __init__(self, session, *, input_stream=None, wait_for_layout=False):
        super().__init__(session, input_stream=input_stream, output=self.say_board)
        self.wait_for_layout = wait_for_layout
        self.replaced = False
        self.pane_columns = None
        self.thinking_since = None
        self.editor = None
        if self.input.isatty() and sys.stdout.isatty():
            from .terminal_input import TerminalInput
            self.editor = TerminalInput(self.input, poll=self.poll, status=self.input_status)

    def input_status(self):
        if self.thinking_since is None:
            return ""
        elapsed = max(0, time.monotonic() - self.thinking_since)
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(elapsed * 8) % 10]
        return f"{spinner} {int(elapsed)}с · "

    def line(self, prompt="Вы > "):
        if self.editor is not None:
            return self.editor.read()
        return super().line(prompt)

    def say_board(self, value, *, label=None):
        if self.editor:
            self.editor.clear()
        board_say(value, label=label, columns=self.pane_columns)
        if self.editor:
            self.editor.draw()

    def await_layout(self):
        # tmux сначала создаёт один широкий терминал, а затем делит его.
        # Читаем ширину и печатаем урок только после окончательного разделения.
        deadline = time.monotonic() + 5
        while True:
            cursor = 0
            while True:
                events = self.session.events(after=cursor, limit=100)
                ready = next((event for event in events if event["event"] == "layout_ready"), None)
                if ready:
                    self.pane_columns = int(ready["data"]["teacher_columns"])
                    return
                if not events:
                    break
                cursor = events[-1]["sequence"]
            if self.session.snapshot()["gate"] == "closed":
                raise ConsoleClosed()
            if time.monotonic() >= deadline:
                raise RuntimeError("Не дождались разделения терминала; повтори запуск сессии")
            time.sleep(.02)

    def poll(self):
        for event in self.session.events(after=self.cursor, limit=100):
            self.cursor = event["sequence"]
            data = event.get("data", {})
            if event["event"] == "teacher_thinking":
                self.thinking_since = time.monotonic()
                self.say("Преподаватель готовит ответ…", label="СТАТУС")
            elif event.get("role") == "teacher":
                if event["event"] in {"teacher_turn_done", "teacher_turn_failed"}:
                    self.thinking_since = None
                _show_teacher_event(event, say=self.say)
            elif event["event"] == "student_question":
                self.say(data["message"], label="ТВОЯ РЕПЛИКА")
            elif event["event"] in {"lesson_intro_shown", "lesson_help_shown"}:
                self.say(data.get("text", "Текст введения отсутствует в этом старом событии. Введи /lesson."), label="УРОК" if event["event"] == "lesson_intro_shown" else "СПРАВКА")
        if self.session.snapshot()["gate"] == "closed":
            raise ConsoleClosed()

    def basics(self):
        try:
            text = _read(self.session.root / "course/TERMINAL_BASICS.md", self.session.root, MAX_TEXT).decode("utf-8")
        except FileNotFoundError:
            text = KEYS + "\nВ области УРОК введи свой вопрос или объяснение; в ЭКСПЕРИМЕНТЕ вводятся команды опытов. /lesson повторяет введение. " + CHAT_NOTE
        self.say(text, label="ОСНОВЫ ТЕРМИНАЛА")

    def run(self):
        self.cursor = _tail(self.session)
        if self.wait_for_layout:
            self.await_layout()
        from .teacher import TeacherRuntime, teacher_backend_label
        self.say(f"Преподаватель подключается через {teacher_backend_label()}. Пиши здесь вопросы обычным текстом. `F3` — лаборатория; `F2` — преподаватель. `/lesson` — материалы; `/retry` — повторить ответ после ошибки.", label="СПРАВКА")
        # Reopening the teacher pane restores its last reply without resetting the lesson.
        history, after = [], 0
        while True:
            batch = self.session.events(after=after, limit=100)
            if not batch:
                break
            history.extend(batch)
            after = batch[-1]["sequence"]
        previous = next((event for event in reversed(history) if event["event"] == "teacher_turn_done"), None)
        if previous:
            _show_teacher_event(previous, say=self.say)
        self.session._record("lesson_board_ready", "system", {})
        teacher = TeacherRuntime(self.session)
        teacher.start()
        try:
            while True:
                try:
                    line = self.line("Вы > ")
                    if not line or line.strip() == "/exit":
                        break
                    if line.strip() == "/lesson":
                        self.session.lesson_intro()
                    elif line.strip() == "/basics":
                        self.basics()
                    elif line.strip() == "/retry":
                        self.session._record("teacher_retry", "student", {})
                    elif line.strip():
                        self.session.ask(line.strip())
                except KeyboardInterrupt:
                    break
                except (ValueError, OSError, RuntimeError) as error:
                    self.say(str(error), label="СООБЩЕНИЕ НЕ ПРИНЯТО")
        except ConsoleClosed:
            pass
        finally:
            teacher.close()
            if not self.replaced and self.session.snapshot()["gate"] != "closed":
                self.session._control("close", role="student")
        return 0


def run_board(session, *, wait_for_layout=False):
    board = LessonBoard(session, wait_for_layout=wait_for_layout)
    previous = {}
    def interrupted(signum, frame):
        board.replaced = True
        raise KeyboardInterrupt()
    for signum in (signal.SIGHUP, signal.SIGTERM):
        previous[signum] = signal.signal(signum, interrupted)
    try:
        return board.run()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def run_help(session, *, content=None, event_prefix="interface_help"):
    """Справка в отдельном popup: ввод в учебных терминалах не изменяется."""
    import curses

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("Справка открывается в интерактивном терминале через Ctrl+h")

    def screen(window):
        offset = 0
        window.timeout(200)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        while session.snapshot()["gate"] != "closed":
            rows, columns = window.getmaxyx()
            lines = board_text(HELP_TEXT if content is None else content, columns=columns, limit=MAX_TEXT).splitlines()
            page = max(1, rows - 2)
            offset = max(0, min(offset, max(0, len(lines) - page)))
            window.erase()
            for row, line in enumerate(lines[offset:offset + page]):
                try:
                    window.addnstr(row, 0, line, max(1, columns - 1))
                except curses.error:
                    pass
            try:
                window.addnstr(rows - 1, 0, " Esc / q — закрыть | стрелки / PgUp / PgDn — читать ", max(1, columns - 1), curses.A_REVERSE)
            except curses.error:
                pass
            window.refresh()
            try:
                key = window.get_wch()
            except curses.error:
                continue
            if key in {"q", "\x1b", "\x08"}:
                return 0
            if key in {curses.KEY_DOWN, curses.KEY_NPAGE}:
                offset += page if key == curses.KEY_NPAGE else 1
            elif key in {curses.KEY_UP, curses.KEY_PPAGE}:
                offset -= page if key == curses.KEY_PPAGE else 1
        return 0

    session._record(event_prefix + "_opened", "student", {"process_id": os.getpid()})
    try:
        return curses.wrapper(screen)
    finally:
        session._record(event_prefix + "_closed", "student", {"process_id": os.getpid()})


def run_result(session):
    cursor, latest = 0, None
    while True:
        batch = session.events(after=cursor, limit=100)
        if not batch:
            break
        for event in batch:
            if event['event'] in {'run_completed', 'structured_compared'}:
                latest = event
        cursor = batch[-1]['sequence']
    if latest and latest['event'] == 'structured_compared':
        from .structured import render_comparison
        report = latest['data']
        return run_help(session, content=render_comparison(report['runs'], report['comparable']), event_prefix="interface_result")
    last = session.snapshot().get("last_run")
    if not last:
        text = "Последний результат\n\nЗапросов ещё не было. Здесь появится сохранённый результат опыта."
    else:
        envelope = last.get("result", {})
        result = envelope.get("result", {})
        if last.get('kind') == 'baseline' and last.get('status') == 'completed':
            from .training_lesson import report_command
            text = report_command(session, []) + '\n\n' + report_command(session, ['1'])
        elif last.get("kind") == "complete" and isinstance(result, dict) and isinstance(result.get("text"), str):
            text = "Ответ модели\n\n" + result["text"]
        else:
            text = "Последний результат\n\n" + terminal_text(envelope)
    return run_help(session, content=text, event_prefix="interface_result")


def configure_interface(server, session, teacher, experiment):
    """Только интерфейс собственного tmux; рабочие данные и процессы сохраняются."""
    if (session._find_lesson(session.snapshot()['lesson'])[0] / 'workspace.json').exists():
        # Keep the command list in one argument: a separate ';' would end bind-key
        # and execute the remaining commands immediately during configuration.
        for table in ('root', 'copy-mode', 'copy-mode-vi'):
            server.command('bind-key', '-T', table, 'F2', f'select-pane -t {experiment} ; send-keys -t {experiment} F2')
            server.command('bind-key', '-T', table, 'F3', f'select-pane -t {experiment} ; send-keys -t {experiment} F3')
            for key in ('F1','F4','F5','F6','F7','F8','F9','F10'):
                server.command('bind-key', '-T', table, key, f'select-pane -t {experiment} ; send-keys -t {experiment} {key}')
        server.command('set-option', '-g', 'status-left', ' F2 преподаватель | F3 лаборатория ')
        server.command('set-option', '-g', 'status-left-length', '45')
        server.command('set-option', '-g', 'status-right', 'F1 помощь | F4 пресеты | F5 выполнить | F6 стоп')
        server.command('set-option', '-g', 'status-right-length', '65')
        server.command('bind-key', '-n', 'C-h', f'select-pane -t {experiment} ; send-keys -t {experiment} F1')
        server.command('select-pane', '-t', teacher, '-T', 'ПРЕПОДАВАТЕЛЬ · F2')
        server.command('select-pane', '-t', experiment, '-T', 'ЛАБОРАТОРИЯ · F3')
        server.command('select-pane', '-t', teacher)
        return
    active = server.command("display-message", "-p", "-t", "course", "#{pane_id}").stdout.strip()
    server.command("set-option", "-g", "status-left-length", "24")
    server.command("set-option", "-g", "status-left", "#[bg=colour24,fg=colour231,bold] Ctrl+h — помощь #[default]")
    server.command("set-option", "-g", "status-right-length", "95")
    learning_key = "F8: обучение | " if session.snapshot()["lesson"] == "04" else ""
    server.command("set-option", "-g", "status-right", "F2: преподаватель | F3: ученик | F4: ответ | " + learning_key + "F9: песочница | F10: редактор ")
    for table in ("root", "copy-mode", "copy-mode-vi"):
        server.command("bind-key", "-T", table, "F2", "select-pane", "-t", teacher)
        server.command("bind-key", "-T", table, "F3", "select-pane", "-t", experiment)
        server.command("bind-key", "-T", table, "F4", "display-popup", "-E", "-w", "90%", "-h", "90%", "--", *child_argv(session, "result"))
        if session.snapshot()["lesson"] == "04" and session._find_lesson('04')[1].get('practice_mode')=='learning-lab-v1':
            server.command("bind-key", "-T", table, "F8", "select-pane", "-t", experiment, ";", "send-keys", "-t", experiment, "F8")
            for key in ('F4','F9'):
                server.command("bind-key", "-T", table, key, "select-pane", "-t", experiment, ";", "send-keys", "-t", experiment, key)
        elif session.snapshot()["lesson"] == "04":
            server.command("bind-key", "-T", table, "F8", "display-popup", "-E", "-w", "100%", "-h", "100%", "--", *child_argv(session, "learning"))
        server.command("bind-key", "-T", table, "F10", "display-popup", "-E", "-w", "100%", "-h", "100%", "--", *child_argv(session, "editor"))
        if not (session.snapshot()["lesson"] == "04" and session._find_lesson('04')[1].get('practice_mode')=='learning-lab-v1'):
            server.command("bind-key", "-T", table, "F9", "display-popup", "-E", "-w", "95%", "-h", "95%", "--", *child_argv(session, "playground"))
    server.command("bind-key", "-n", "C-h", "display-popup", "-E", "-w", "80%", "-h", "80%", "--", *child_argv(session, "help"))
    for table in ("copy-mode", "copy-mode-vi"):
        server.command("bind-key", "-T", table, "C-h", "display-popup", "-E", "-w", "80%", "-h", "80%", "--", *child_argv(session, "help"))
    server.command("bind-key", "o", "select-pane", "-t", ":.+")
    server.command("select-pane", "-t", teacher, "-T", "ТЕРМИНАЛ ПРЕПОДАВАТЕЛЯ · УРОК")
    server.command("select-pane", "-t", experiment, "-T", "ТЕРМИНАЛ УЧЕНИКА · ЭКСПЕРИМЕНТ")
    if active in {teacher, experiment}:
        server.command("select-pane", "-t", active)


def refresh_interface(session, *, redraw=False):
    state = session.snapshot()
    if state["gate"] == "closed" or state["owner_pid"] is None:
        raise RuntimeError("Нет открытого учебного окна для обновления")
    cursor, layout = 0, None
    while True:
        events = session.events(after=cursor, limit=100)
        if not events:
            break
        for event in events:
            if event["event"] == "layout_started":
                layout = event["data"]
        cursor = events[-1]["sequence"]
    if not layout:
        raise RuntimeError("В этой сессии нет разделённого окна tmux")
    socket = Path(layout["socket"])
    if socket.name != "socket" or socket.parent.parent != Path("/tmp") or not socket.parent.name.startswith("llm-course-tmux-") or socket.parent.is_symlink() or socket.is_symlink():
        raise ValueError("Нужен собственный socket учебной сессии")
    executable = shutil.which("tmux")
    if not executable:
        raise RuntimeError("tmux не найден")
    server = TmuxServer(executable, socket)
    for key, mode in (("teacher_pane", "board"), ("experiment_pane", "experiment")):
        pane = layout[key]
        if not re.fullmatch(r"%\d+", pane):
            raise ValueError("Неверный идентификатор учебного терминала")
        command = server.command("display-message", "-p", "-t", pane, "#{pane_start_command}").stdout.strip()
        args = shlex.split(command)
        if len(args) < 8 or args[:3] != [sys.executable, "-I", "-c"] or args[-4:] != child_argv(session, mode)[-4:]:
            raise ValueError("Терминал не принадлежит этой учебной сессии")
    configure_interface(server, session, layout["teacher_pane"], layout["experiment_pane"])
    if redraw:
        active = server.command("display-message", "-p", "-t", "course", "#{pane_id}").stdout.strip()
        server.command("respawn-pane", "-k", "-t", layout["teacher_pane"], "-c", str(session.root), "--", *child_argv(session, "board"))
        if active in {layout["teacher_pane"], layout["experiment_pane"]}:
            server.command("select-pane", "-t", active)
    session._record("interface_updated", "system", {"updated_by": "teacher", "help_key": "Ctrl+h", "teacher_key": "F2", "student_key": "F3"})
    return {"updated": True, "session": session.id, "help_key": "Ctrl+h", "teacher_key": "F2", "student_key": "F3", "board_redrawn": redraw}


class TmuxServer:
    def __init__(self, executable, socket):
        self.prefix = [executable, "-f", "/dev/null", "-S", str(socket)]
        self.environment = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(Path.home()),
                            "LANG": "C.UTF-8", "TERM": os.environ.get("TERM", "xterm-256color")}
        for name in ("COURSE_TEACHER_COMMAND", "COURSE_TEACHER_BACKEND", "COURSE_TEACHER_API_MODEL",
                     "COURSE_TEACHER_API_PROXY", "COURSE_TEACHER_API_ENDPOINT", "COURSE_TEACHER_MODEL",
                     "OPENAI_API_KEY", "CODEX_HOME"):
            if os.environ.get(name):
                self.environment[name] = os.environ[name]
        self.process = None

    def command(self, *arguments, check=True):
        result = subprocess.run([*self.prefix, *arguments], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, env=self.environment)
        if check and result.returncode:
            raise RuntimeError("tmux: " + result.stderr.strip()[:1000])
        return result

    def start(self):
        self.process = subprocess.Popen([*self.prefix, "-D"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.environment, start_new_session=True)
        deadline = time.monotonic() + 3
        while not Path(self.prefix[-1]).exists():
            if self.process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("Не удалось запустить отдельный сервер tmux")
            time.sleep(.02)

    def stop(self):
        if self.process is None:
            return
        try:
            if self.process.poll() is None:
                try:
                    self.command("kill-server", check=False)
                except (OSError, subprocess.TimeoutExpired):
                    pass  # Ниже остаётся собственный живой Popen handle.
        finally:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)


def _quarantine(session, reason):
    with session._lock("metadata.lock"):
        state = session._state()
        state["recovery_required"] = True
        state["gate"] = "closed"
        session._save(state)
    session._record("layout_cleanup_uncertain", "system", {"reason": reason})


def _cleanup(session, server):
    confirmed = False
    problem = None
    try:
        session._control("close", role="student")
        deadline = time.monotonic() + 12
        while session.snapshot()["active_run"] is not None and time.monotonic() < deadline:
            time.sleep(.05)
        confirmed = session.snapshot()["active_run"] is None
        if not confirmed:
            problem = "Остановка активного опыта не подтверждена"
            _quarantine(session, problem)
        deadline = time.monotonic() + 3
        while session.snapshot()["owner_pid"] is not None and time.monotonic() < deadline:
            time.sleep(.05)
    except (ValueError, OSError, RuntimeError) as error:
        problem = str(error)
        try:
            _quarantine(session, problem)
        except (ValueError, OSError, RuntimeError):
            problem += "; запись карантина также не подтверждена"
    finally:
        server.stop()
    deadline = time.monotonic() + 3
    while session.snapshot()["owner_pid"] is not None and time.monotonic() < deadline:
        time.sleep(.05)
    state = session.snapshot()
    released = state["owner_pid"] is None
    if not released:
        problem = "Освобождение владельца опыта не подтверждено"
        _quarantine(session, problem)
        state = session.snapshot()
    session._record("layout_closed", "system", {"active_run_cleared": confirmed, "owner_released": released, "owned_tmux_stopped": server.process is None or server.process.poll() is not None, "recovery_required": state["recovery_required"]})
    if problem or state["recovery_required"]:
        raise RuntimeError((problem or "Исход прежнего действия требует сверки") + "; сессия закрыта, журнал сохранён")


@contextmanager
def _signals():
    previous = {}

    def interrupt(signum, frame):
        raise KeyboardInterrupt()

    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, interrupt)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run_split(session):
    import importlib.util

    if not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get("TERM", "") in {"", "dumb", "unknown"}:
        raise RuntimeError("Две области требуют обычного интерактивного терминала с TERM")
    executable = shutil.which("tmux")
    if not executable:
        raise RuntimeError("Для двух областей нужен установленный tmux. Автоматическая установка не выполняется")
    if importlib.util.find_spec("rich") is None:
        raise RuntimeError("Для оформления терминала преподавателя нужен rich из requirements.txt")
    size = shutil.get_terminal_size()
    if size.columns < 80 or size.lines < 18:
        raise RuntimeError("Увеличь терминал хотя бы до 80×18, затем повтори подключение")
    state = session.snapshot()
    if state["gate"] == "closed" or state["owner_pid"] is not None:
        raise RuntimeError("Нужна открытая сессия без другого терминала ученика")
    if (session._find_lesson(state['lesson'])[0] / 'workspace.json').exists():
        if size.columns < 100 or size.lines < 26:
            raise RuntimeError('Для преподавателя и лаборатории увеличь терминал хотя бы до 100×26.')
        from .workbench import Workspace
        Workspace(session)
    result, client = 0, None
    with tempfile.TemporaryDirectory(prefix="llm-course-tmux-", dir="/tmp") as temporary, _signals():
        socket = Path(temporary) / "socket"
        server = TmuxServer(executable, socket)
        try:
            server.start()
            teacher = server.command("new-session", "-d", "-s", "course", "-x", str(size.columns), "-y", str(size.lines), "-c", str(session.root), "-P", "-F", "#{pane_id}", "--", *child_argv(session, "board")).stdout.strip()
            server.command("set-option", "-g", "default-shell", "/bin/sh")
            server.command("set-option", "-g", "update-environment", "")
            server.command("set-option", "-g", "mouse", "on")
            server.command("set-option", "-g", "history-limit", "10000")
            server.command("set-option", "-g", "pane-border-status", "top")
            server.command("set-option", "-g", "pane-border-format", " #{pane_title} ")
            experiment = server.command("split-window", "-h" if size.columns >= 100 else "-v", "-t", teacher, "-c", str(session.root), "-P", "-F", "#{pane_id}", "--", *child_argv(session, "experiment")).stdout.strip()
            configure_interface(server, session, teacher, experiment)
            server.command("select-pane", "-t", teacher)
            session._record("layout_started", "system", {"backend": "tmux", "socket": str(socket), "teacher_pane": teacher, "experiment_pane": experiment, "owned_server_pid": server.process.pid})
            client = subprocess.Popen([*server.prefix, "attach-session", "-t", "course"], env=server.environment)
            # Подключение клиента может ещё раз изменить размеры терминалов.
            # Сигнализируем готовность после этого, с шириной от самого tmux.
            deadline = time.monotonic() + 5
            while True:
                if client.poll() is not None:
                    raise RuntimeError("Клиент tmux завершился до открытия учебного окна")
                attached = server.command("display-message", "-p", "-t", "course", "#{session_attached}").stdout.strip()
                if attached.isdigit() and int(attached) > 0:
                    columns = int(server.command("display-message", "-p", "-t", teacher, "#{pane_width}").stdout.strip())
                    session._record("layout_ready", "system", {"teacher_columns": columns})
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Не удалось подключить клиент tmux")
                time.sleep(.02)
            result = client.wait()
        except KeyboardInterrupt:
            result = 130
        finally:
            ignored = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
            try:
                if client is not None and client.poll() is None:
                    client.terminate()
                    try:
                        client.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        client.kill()
                        client.wait(timeout=2)
                _cleanup(session, server)
            finally:
                for sig, handler in ignored.items():
                    signal.signal(sig, handler)
    return result
