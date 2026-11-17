"""CLI курса. Полный запрос и каждое действие доступны в журнале JSON."""

import argparse
import json
import platform
import shutil
import sys
from pathlib import Path

from .model import ModelClient
from .registry import ROOT, asset_path, catalog, lesson, validate_course
from .trace import Trace


def _json_file(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _print(value, output=None):
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(text, end="")


def _connection(parser):
    parser.add_argument("--endpoint", default="http://127.0.0.1:8088")
    parser.add_argument("--allow-remote", action="store_true", help="Явно разрешить свой внешний сервер модели")
    parser.add_argument("--timeout", type=float, default=60)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "classroom":
        from .classroom_console import main as classroom_main
        return classroom_main(arguments[1:])
    parser = argparse.ArgumentParser(description="Практический курс: отдельное ядро модели и прозрачный harness")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("classroom", help="Терминал ученика и наблюдение, советы и управление преподавателя")
    commands.add_parser("doctor", help="Проверить интерпретатор и наличие компонентов, не запускать сервисы")
    commands.add_parser("catalog", help="Показать занятия и отдельные статусы проверок")
    commands.add_parser("validate", help="Проверить структуру материалов без модели")
    prompt = commands.add_parser("prompt", help="Напечатать точный вход выбранного опыта")
    prompt.add_argument("lesson")
    prompt.add_argument("experiment")

    complete = commands.add_parser("complete", help="Один запрос текста к серверу llama.cpp")
    complete.add_argument("--prompt-file", required=True)
    complete.add_argument("--max-tokens", type=int, default=256)
    complete.add_argument("--temperature", type=float, default=0)
    complete.add_argument("--seed", type=int, default=42)
    complete.add_argument("--schema")
    complete.add_argument("--output")
    _connection(complete)

    check = commands.add_parser("check-json", help="Проверить синтаксис и JSON Schema, не истинность содержимого")
    check.add_argument("--schema", required=True)
    check.add_argument("--input", required=True)

    agent = commands.add_parser("agent", help="Запустить ограниченный цикл с явно разрешёнными инструментами")
    agent.add_argument("--task-file")
    agent.add_argument("--workspace", required=True)
    agent.add_argument("--max-steps", type=int, default=8)
    agent.add_argument("--max-requests", type=int)
    agent.add_argument("--max-tokens", type=int, default=2048)
    agent.add_argument("--temperature", type=float, default=0.0)
    agent.add_argument("--seed", type=int, default=42)
    agent.add_argument("--deadline-seconds", type=float, default=180)
    agent.add_argument("--skill", action="append", default=[])
    agent.add_argument("--state")
    agent.add_argument("--resume")
    agent.add_argument("--answer-file")
    agent.add_argument("--mcp", action="store_true")
    agent.add_argument("--tools", default="read_file,write_file")
    agent.add_argument("--sandbox-image", default="bash:5.2")
    _connection(agent)

    sandbox = commands.add_parser("sandbox-check", help="Проверить подготовленный контейнер без загрузки образов")
    sandbox.add_argument("--workspace", required=True)
    sandbox.add_argument("--sandbox-image", default="bash:5.2")

    mcp = commands.add_parser("mcp-demo", help="Настоящий локальный MCP через отдельный stdio процесс")
    mcp.add_argument("--workspace", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            import importlib.util
            _print({
                "python": platform.python_version(),
                "python_executable": sys.executable,
                "jsonschema": importlib.util.find_spec("jsonschema") is not None,
                "docker_cli": shutil.which("docker"),
                "docker_daemon_and_images": "not_checked",
                "llama_server": str(ROOT / "runtime/llama-b12380/llama-server") if (ROOT / "runtime/llama-b12380/llama-server").is_file() else shutil.which("llama-server"),
                "local_weights": [path.name for path in (ROOT / "models").glob("*.gguf") if path.is_file()],
                "training_dependencies": {name: importlib.util.find_spec(name) is not None for name in ("torch", "transformers", "peft")},
                "network_requests": 0,
            })
            return 0
        if args.command == "catalog":
            _print([{key: item[key] for key in ("id", "title", "duration_minutes", "status", "directory")} for item in catalog()])
            return 0
        if args.command == "validate":
            errors = validate_course()
            _print({"check": "course_structure_only", "lessons": len(catalog()), "ok": not errors, "errors": errors})
            return 1 if errors else 0
        if args.command == "prompt":
            item = lesson(args.lesson)
            matches = [experiment for experiment in item["experiments"] if experiment["id"] == args.experiment]
            if len(matches) != 1:
                raise ValueError("Не найдено ровно одного опыта с этим id")
            if "prompt_file" not in matches[0]:
                raise ValueError("У этого опыта нет запроса модели; следуйте инструкции занятия")
            print(asset_path(ROOT / item["directory"], matches[0]["prompt_file"]).read_text(encoding="utf-8"), end="")
            return 0
        if args.command == "check-json":
            from jsonschema import Draft202012Validator
            schema = _json_file(args.schema)
            Draft202012Validator.check_schema(schema)
            value = _json_file(args.input)
            errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda error: str(error.path))
            _print({"valid": not errors, "check": "syntax_and_schema_only", "errors": [{"path": list(error.path), "message": error.message} for error in errors]})
            return 1 if errors else 0
        if args.command == "complete":
            trace = Trace()
            client = ModelClient(args.endpoint, allow_remote=args.allow_remote, timeout=args.timeout, trace=trace)
            result = client.generate(
                Path(args.prompt_file).read_text(encoding="utf-8"), max_tokens=args.max_tokens,
                temperature=args.temperature, seed=args.seed,
                json_schema=_json_file(args.schema) if args.schema else None,
            )
            result["events"] = trace.events
            _print(result, args.output)
            return 0
        if args.command == "agent":
            from .agent import run_agent
            if not args.task_file and not args.resume:
                parser.error("agent требует --task-file либо --resume")
            if args.answer_file and not args.resume:
                parser.error("--answer-file допустим только вместе с --resume")
            task = Path(args.task_file).read_text(encoding="utf-8") if args.task_file else ""
            skills = [Path(path).read_text(encoding="utf-8") for path in args.skill]
            answer = Path(args.answer_file).read_text(encoding="utf-8") if args.answer_file else None
            trace = Trace()
            client = ModelClient(args.endpoint, allow_remote=args.allow_remote, timeout=args.timeout, trace=trace)
            result = run_agent(
                client, task, args.workspace, max_steps=args.max_steps, trace=trace,
                skills=skills, state_path=args.state, resume_path=args.resume, answer=answer,
                mcp=args.mcp, allowed_tools=[name.strip() for name in args.tools.split(",") if name.strip()],
                sandbox_image=args.sandbox_image, max_requests=args.max_requests,
                deadline_seconds=args.deadline_seconds,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                seed=args.seed,
            )
            _print(result)
            return 0 if result["status"] in {"finished", "waiting_for_user"} else 1
        if args.command == "sandbox-check":
            from .sandbox import DockerSandbox
            workspace = Path(args.workspace).resolve()
            workspace.mkdir(parents=True, exist_ok=True)
            result = DockerSandbox(image=args.sandbox_image).run(workspace, "printf 'sandbox-ok\\n'; test ! -w /etc; test ! -S /var/run/docker.sock")
            _print(result)
            return 0 if result.get("exit_code") == 0 and not result.get("timed_out") else 1
        if args.command == "mcp-demo":
            from .mcp import LocalMCPClient
            trace = Trace()
            with LocalMCPClient(args.workspace, trace=trace) as client:
                tools = client.list_tools()
                result = client.call_tool("read_note", {"path": "notes.txt"})
            _print({"tools": tools, "result": result, "events": trace.events})
            return 1 if result.get("isError") else 0
    except (ValueError, OSError, RuntimeError, ImportError) as error:
        _print({"ok": False, "error": str(error), "error_type": type(error).__name__})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
