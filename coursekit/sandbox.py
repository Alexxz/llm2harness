"""Docker как граница исполнения; проверенный снимок как граница записи на хост.

В контейнер передаётся только read-only вход. Работа идёт в ограниченном tmpfs.
После остановки процессов проверенные обычные файлы возвращаются через политику.
"""

from dataclasses import dataclass
import io
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid

from .policy import MAX_FILE_BYTES, MAX_FILES, MAX_WORKSPACE_BYTES, PolicyError, WorkspacePolicy, _relative_parts


OUTPUT_LIMIT = 64 * 1024
ARCHIVE_LIMIT = MAX_WORKSPACE_BYTES + 2 * 1024 * 1024


class SandboxError(RuntimeError):
    def __init__(self, message, *, outcome_unknown=False):
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


@dataclass
class Captured:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_limit: bool = False


def _capture(command, timeout, *, limit=OUTPUT_LIMIT):
    """Ограничивает и время процесса Docker CLI, и размер двух каналов вывода."""
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    started, count = time.monotonic(), 0
    timed_out = output_limit = False
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        try:
            while selector.get_map():
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    timed_out = True
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    available = max(0, limit - count)
                    buffers[key.data].extend(chunk[:available])
                    count += len(chunk)
                    if count > limit:
                        output_limit = True
                        break
                if output_limit:
                    break
            if timed_out or output_limit:
                process.kill()
            process.wait(timeout=max(0.1, timeout - (time.monotonic() - started)))
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait(timeout=2)
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            raise
        finally:
            process.stdout.close()
            process.stderr.close()
    return Captured(process.returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"]), timed_out, output_limit)


def _snapshot_from_tar(payload):
    """Не распаковывает недоверенный tar на диск."""
    if len(payload) > ARCHIVE_LIMIT:
        raise PolicyError("Экспорт контейнера превышает предел")
    result, total, entries = {}, 0, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            for member in archive:
                entries += 1
                if entries > MAX_FILES * 2:
                    raise PolicyError("Слишком много элементов в экспорте")
                name = member.name
                while name.startswith("./"):
                    name = name[2:]
                if name in {"", "."} and member.isdir():
                    continue
                if member.isdir():
                    _relative_parts(name.rstrip("/"))
                    continue
                _relative_parts(name)
                if not member.isfile() or member.size > MAX_FILE_BYTES or name in result:
                    raise PolicyError("В экспорте допустимы только ограниченные обычные файлы без повторов")
                total += member.size
                if len(result) >= MAX_FILES or total > MAX_WORKSPACE_BYTES:
                    raise PolicyError("Экспорт превышает предел файлов или объёма")
                stream = archive.extractfile(member)
                result[name] = stream.read(MAX_FILE_BYTES + 1)
                if len(result[name]) != member.size:
                    raise PolicyError("Неполный файл в экспорте")
    except (tarfile.TarError, OSError) as error:
        raise PolicyError("Неправильный архив экспорта") from error
    for name in result:
        parts = name.split("/")
        if any("/".join(parts[:i]) in result for i in range(1, len(parts))):
            raise PolicyError("Файл в экспорте используется как каталог")
    return result


class DockerSandbox:
    def __init__(self, *, image="bash:5.2", timeout=10):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}", image):
            raise ValueError("Неправильное имя образа")
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
            raise ValueError("Таймаут контейнера должен быть в пределах 0..300 секунд")
        self.image, self.timeout = image, timeout

    def run(self, workspace, script):
        if not isinstance(script, str) or len(script.encode("utf-8")) > MAX_FILE_BYTES:
            raise PolicyError("Скрипт превышает учебный предел")
        policy = WorkspacePolicy(workspace)
        if any(character in str(policy.root) for character in ",\n\r"):
            raise PolicyError("Путь рабочего каталога нельзя безопасно передать Docker mount")
        original = policy.snapshot()
        docker = shutil.which("docker", path="/usr/bin:/bin")
        if not docker:
            raise SandboxError("Docker CLI недоступен; исполнение на хосте запрещено")
        name = "coursekit-" + uuid.uuid4().hex
        deadline = time.monotonic() + self.timeout
        created = False
        result = None
        with tempfile.TemporaryDirectory(prefix="coursekit-docker-") as config:
            base = [docker, "--config", config, "--host", "unix:///var/run/docker.sock"]

            def call(arguments, *, limit=OUTPUT_LIMIT):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return Captured(None, b"", b"", timed_out=True)
                return _capture(base + arguments, remaining, limit=limit)

            try:
                inspected = call(["image", "inspect", "--format", "{{.Id}}", self.image])
                image_id = inspected.stdout.decode("utf-8", errors="replace").strip()
                if inspected.exit_code != 0 or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                    raise SandboxError("Подготовленный образ или локальный Docker daemon недоступен; загрузки нет")
                command = [
                    "run", "--detach", "--name", name, "--pull", "never", "--init",
                    "--network", "none", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges:true", "--pids-limit", "64",
                    "--memory", "256m", "--memory-swap", "256m", "--cpus", "1",
                    "--ulimit", "nofile=128:128", "--user", f"{os.getuid() or 65534}:{os.getgid() or 65534}",
                    "--workdir", "/work", "--mount", f"type=bind,source={policy.root},target=/course-input,readonly",
                    "--tmpfs", "/work:rw,nosuid,nodev,noexec,size=32m,mode=1777",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777",
                    "--env", "XDG_CONFIG_HOME=/tmp/coursekit-config", "--env", "TERM=dumb",
                    image_id, "bash", "--noprofile", "--norc", "-c", "while :; do sleep 3600; done",
                ]
                # Mark the name before starting: a CLI timeout may still create the container.
                created = True
                started = call(command)
                if started.exit_code != 0 or started.timed_out:
                    raise SandboxError("Контейнер не запустился")
                copied = call(["exec", name, "bash", "--noprofile", "--norc", "-c", "cp -R /course-input/. /work/"])
                if copied.exit_code != 0:
                    raise SandboxError("Входной снимок не помещается в контейнер или недоступен")
                execution = call(["exec", name, "bash", "--noprofile", "--norc", "-c", script])
                result = {
                    "exit_code": execution.exit_code,
                    "stdout": execution.stdout.decode("utf-8", errors="replace"),
                    "stderr": execution.stderr.decode("utf-8", errors="replace"),
                    "timed_out": execution.timed_out,
                    "output_limit": execution.output_limit,
                    "changed_files": [],
                    "snapshot_applied": False,
                    "image_id": image_id,
                }
                if execution.timed_out or execution.output_limit:
                    return result
                paused = call(["pause", name])
                if paused.exit_code != 0:
                    raise SandboxError("Не удалось заморозить процессы перед экспортом")
                exported = call(["cp", name + ":/work/.", "-"], limit=ARCHIVE_LIMIT)
                if exported.exit_code != 0 or exported.timed_out or exported.output_limit:
                    raise SandboxError("Не удалось получить ограниченный снимок контейнера")
                snapshot = _snapshot_from_tar(exported.stdout)
                if original.keys() - snapshot.keys():
                    raise PolicyError("Удаление исходных файлов не поддерживается; снимок не применён")
                if policy.snapshot() != original:
                    raise PolicyError("Рабочий каталог изменён параллельно; снимок не применён")
                changed = sorted(name for name, data in snapshot.items() if original.get(name) != data)
                try:
                    for path in changed:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Истёк срок применения снимка")
                        policy.write_bytes(path, snapshot[path])
                        result["changed_files"].append(path)
                except Exception as error:
                    raise SandboxError("Применение снимка прервано; исход записей требует проверки", outcome_unknown=True) from error
                result["snapshot_applied"] = True
                return result
            finally:
                if created:
                    # Independent small cleanup budget also applies when the execution deadline expired.
                    cleaned = _capture(base + ["rm", "--force", name], 5)
                    if cleaned.exit_code != 0:
                        raise SandboxError(f"Не подтверждено удаление контейнера {name}; требуется проверка", outcome_unknown=True)
