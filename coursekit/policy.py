"""Политика файловых инструментов. Генерация текста сама ничего не исполняет."""

import os
from pathlib import Path
import re
import secrets
import stat
import tempfile


MAX_FILE_BYTES = 128 * 1024
MAX_WORKSPACE_BYTES = 16 * 1024 * 1024
MAX_FILES = 256
DEFAULT_TOOLS = frozenset({"read_file", "write_file"})
TOOL_NAMES = frozenset({"read_file", "write_file", "run_bash", "mcp_call"})
CONTROL_ACTIONS = frozenset({"ask_user", "finish"})


class PolicyError(ValueError):
    """Отказ до выполнения действия с известной причиной."""


def allowed_tool_names(names=None):
    result = DEFAULT_TOOLS if names is None else frozenset(names)
    if result - TOOL_NAMES - CONTROL_ACTIONS:
        raise PolicyError("Неизвестный инструмент в разрешениях")
    return result - CONTROL_ACTIONS


def validate_action(value, allowed_tools=None):
    if not isinstance(value, dict) or set(value) != {"action", "arguments"}:
        raise PolicyError("Действие должно содержать только action и arguments")
    name, arguments = value["action"], value["arguments"]
    if not isinstance(name, str) or not isinstance(arguments, dict):
        raise PolicyError("Неправильные типы action или arguments")
    if name not in allowed_tool_names(allowed_tools) | CONTROL_ACTIONS:
        raise PolicyError("Действие не разрешено политикой")
    fields = {
        "read_file": {"path": str},
        "write_file": {"path": str, "content": str},
        "run_bash": {"script": str},
        "mcp_call": {"name": str, "arguments": dict},
        "ask_user": {"questions": str},
        "finish": {"answer": str},
    }[name]
    if set(arguments) != set(fields) or any(type(arguments[k]) is not t for k, t in fields.items()):
        raise PolicyError("Аргументы действия не соответствуют его контракту")
    if any(isinstance(v, str) and len(v.encode("utf-8")) > MAX_FILE_BYTES for v in arguments.values()):
        raise PolicyError("Аргумент превышает учебный предел 128 KiB")
    if name == "ask_user" and not arguments["questions"].strip():
        raise PolicyError("Раунд вопросов не может быть пустым")
    return value


def _relative_parts(path):
    if not isinstance(path, str) or not path or len(path) > 1024 or "\x00" in path or "\\" in path:
        raise PolicyError("Нужен непустой относительный путь")
    parts = path.split("/")
    openspec_metadata = (
        len(parts) == 4 and parts[:2] == ["openspec", "changes"]
        and re.fullmatch(r"[a-z0-9][a-z0-9-]*", parts[2]) is not None
        and parts[3] == ".openspec.yaml"
    )
    hidden = any(p.startswith(".") for p in parts) and not openspec_metadata
    if any(not p or p in {".", ".."} for p in parts) or hidden or path.startswith("/"):
        raise PolicyError("Абсолютные, скрытые пути и переходы к родителю запрещены")
    return parts


class WorkspacePolicy:
    """Доступ через открытые каталоги и O_NOFOLLOW, без следования ссылкам."""

    def __init__(self, workspace):
        root = Path(workspace).absolute()
        if root == Path(root.anchor) or any(p.startswith(".") for p in root.parts if p not in {"/", ""}):
            raise PolicyError("Нужен отдельный видимый рабочий каталог")
        if root.resolve() != root:
            raise PolicyError("Рабочий каталог и его предки не должны быть ссылками")
        if root == Path(tempfile.gettempdir()).resolve():
            raise PolicyError("Нужен отдельный каталог, а не общий временный каталог")
        trusted_root = Path(__file__).resolve().parent.parent
        for name in ("coursekit", "skills", "tools", "tests", "containers", "lessons"):
            trusted = trusted_root / name
            if trusted.is_relative_to(root) or root.is_relative_to(trusted):
                raise PolicyError("Рабочий каталог пересекается с доверенным кодом или материалами курса")
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise PolicyError("Рабочий путь должен быть каталогом")
        self.root = root

    def _parent(self, path, *, create=False):
        parts = _relative_parts(path)
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o755, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor, parts[-1]
        except (OSError, ValueError) as error:
            os.close(descriptor)
            raise PolicyError("Каталог недоступен или содержит ссылку") from error

    @staticmethod
    def _regular(info):
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PolicyError("Допустим только обычный файл без ссылок")
        if info.st_size > MAX_FILE_BYTES:
            raise PolicyError("Файл превышает 128 KiB")

    def read_bytes(self, path):
        parent, name = self._parent(path)
        try:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(fd, "rb") as stream:
                    self._regular(os.fstat(stream.fileno()))
                    result = stream.read(MAX_FILE_BYTES + 1)
                if len(result) > MAX_FILE_BYTES:
                    raise PolicyError("Файл вырос за предел 128 KiB")
                return result
            except OSError as error:
                raise PolicyError("Файл недоступен или является ссылкой") from error
        finally:
            os.close(parent)

    def read_file(self, path):
        try:
            return self.read_bytes(path).decode("utf-8")
        except UnicodeDecodeError as error:
            raise PolicyError("Файловый инструмент читает только UTF-8") from error

    def write_bytes(self, path, data):
        if len(data) > MAX_FILE_BYTES:
            raise PolicyError("Запись превышает 128 KiB")
        parts = _relative_parts(path)
        sizes, directories = self._inventory()
        if path not in sizes and len(sizes) >= MAX_FILES:
            raise PolicyError("Превышен предел файлов рабочего каталога")
        if sum(sizes.values()) - sizes.get(path, 0) + len(data) > MAX_WORKSPACE_BYTES:
            raise PolicyError("Превышен общий объём рабочего каталога")
        needed = {"/".join(parts[:i]) for i in range(1, len(parts))} - directories
        if len(directories) + len(needed) > MAX_FILES:
            raise PolicyError("Превышен предел каталогов рабочего пространства")
        parent, name = self._parent(path, create=True)
        temporary = "coursekit-write-" + secrets.token_hex(10)
        try:
            try:
                self._regular(os.stat(name, dir_fd=parent, follow_symlinks=False))
            except FileNotFoundError:
                pass
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        finally:
            os.close(parent)

    def write_file(self, path, content):
        data = content.encode("utf-8")
        self.write_bytes(path, data)
        return {"path": path, "bytes": len(data)}

    def _inventory(self):
        sizes, directories, total = {}, {""}, 0
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            for child in dirs:
                relative = (Path(directory) / child).relative_to(self.root).as_posix()
                _relative_parts(relative)
                if (Path(directory) / child).is_symlink():
                    raise PolicyError("Ссылка на каталог запрещена")
                directories.add(relative)
                if len(directories) > MAX_FILES:
                    raise PolicyError("Превышен предел каталогов")
            for child in files:
                relative = (Path(directory) / child).relative_to(self.root).as_posix()
                _relative_parts(relative)
                info = (Path(directory) / child).lstat()
                self._regular(info)
                total += info.st_size
                if len(sizes) >= MAX_FILES or total > MAX_WORKSPACE_BYTES:
                    raise PolicyError("Превышен предел файлов или объём рабочего каталога")
                sizes[relative] = info.st_size
        return sizes, directories

    def snapshot(self):
        """Ограниченный набор обычных файлов для входа и экспорта контейнера."""
        sizes, _ = self._inventory()
        return {relative: self.read_bytes(relative) for relative in sorted(sizes)}
