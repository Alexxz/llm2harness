"""Фиксированный доверенный worker одного опыта. Протокол — защищённые файлы."""

import argparse
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import platform
import signal
import shutil
import sys


# -I excludes workspace and user startup paths; only this fixed source tree is added.
SOURCE_ROOT = Path(__file__).resolve().parent.parent
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from coursekit.classroom import Session, MAX_RESULT_BYTES, _atomic, _read, _run_settings
from coursekit.trace import Trace


class Cancelled(BaseException):
    """Separate from Exception so agent error handling cannot swallow cancellation."""


_cancelling = False


def _cancel(signum, frame):
    global _cancelling
    if not _cancelling:
        _cancelling = True
        raise Cancelled("Отмена принадлежащего терминалу worker")


def _parent_death_signal(expected_parent_pid):
    if os.getppid() != expected_parent_pid:
        raise Cancelled("Владелец завершился до назначения сигнала")
    if sys.platform != "linux":
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        return False
    if os.getppid() != expected_parent_pid:
        raise Cancelled("Владелец завершился до запуска опыта")
    return True


class JournalTrace(Trace):
    def __init__(self, session, run_id):
        super().__init__()
        self.session, self.run_id = session, run_id

    def emit(self, event, **data):
        item = super().emit(event, **data)
        role = "model" if event.startswith("model_") else "worker"
        self.session._record(event, role, {"run_id": self.run_id, **data})
        return item


def _doctor(root):
    weights = root / "models"
    return {"python": platform.python_version(), "python_executable": sys.executable,
            "jsonschema": importlib.util.find_spec("jsonschema") is not None,
            "docker_cli": shutil.which("docker", path=os.defpath), "docker_daemon_and_images": "not_checked",
            "llama_server": str(root / "runtime/llama-b12380/llama-server") if (root / "runtime/llama-b12380/llama-server").is_file() else None,
            "local_weights": [path.name for path in weights.glob("*.gguf") if not path.is_symlink() and path.is_file()] if weights.is_dir() else [],
            "network_requests": 0}


def _execute(request, session, trace):
    from coursekit.model import ModelClient

    settings, kind = request["settings"], request["kind"]
    _run_settings(kind, settings, request["capabilities"])
    needed = "complete" if kind in {"baseline", "learn"} else "agent" if kind == "continue" else kind
    if needed not in request["capabilities"]:
        raise ValueError("Возможность опыта не была разрешена в зафиксированном запросе")
    if kind == "learn":
        from coursekit.learning_lab import execute
        return "completed", execute(request, session, trace)
    if kind == "baseline":
        from coursekit.training_lesson import baseline
        return "completed", baseline(request, session, trace)
    if kind == "doctor":
        return "completed", _doctor(session.root)
    if kind in {"complete", "agent", "continue"}:
        client = ModelClient(request["endpoint"], allow_remote=request["allow_remote"], timeout=min(settings["timeout"], settings["deadline_seconds"]), trace=trace)
        if kind == "complete":
            return "completed", client.generate(request["prompt"], max_tokens=settings["max_tokens"], temperature=settings["temperature"], seed=settings["seed"], json_schema=request["schema"])
        from coursekit.agent import run_agent
        result = run_agent(
            client, "" if kind == "continue" else request["prompt"], session.workspace,
            max_steps=settings["max_steps"], trace=trace, skills=request["skills"],
            state_path=request["agent_state_path"] if kind == "agent" else None,
            resume_path=request["agent_state_path"] if kind == "continue" else None,
            answer=request["prompt"] if kind == "continue" else None,
            mcp="mcp_call" in settings["tools"], allowed_tools=settings["tools"],
            sandbox_image=request["sandbox_image"], max_requests=settings["max_requests"],
            deadline_seconds=settings["deadline_seconds"], max_tokens=settings["max_tokens"],
            temperature=settings["temperature"], seed=settings["seed"],
        )
        # Full authoritative state is already protected outside the workspace.
        summary = {key: value for key, value in result.items() if key not in {"events", "state"}}
        return result["status"], summary
    if kind == "mcp":
        from coursekit.mcp import LocalMCPClient
        with LocalMCPClient(session.workspace, trace=trace, timeout=min(10, settings["timeout"])) as client:
            tools = client.list_tools()
            result = client.call_tool("read_note", {"path": "notes.txt"})
        return "tool_failed" if result["isError"] else "completed", {"tools": tools, "result": result}
    if kind == "bash":
        from coursekit.sandbox import DockerSandbox
        result = DockerSandbox(image=request["sandbox_image"], timeout=min(300, settings["timeout"], settings["deadline_seconds"])).run(session.workspace, request["prompt"])
        trace.emit("sandbox_result", result=result)
        status = "completed" if result["exit_code"] == 0 and not result["timed_out"] and not result["output_limit"] else "tool_failed"
        return status, result
    raise ValueError("Неизвестный вид опыта")


def _pending(request, session):
    path = Path(request["agent_state_path"])
    if not path.exists():
        return False
    try:
        return bool(json.loads(_read(path, session.directory, MAX_RESULT_BYTES)).get("pending"))
    except (ValueError, OSError):
        return True


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    # Parent selects this protected file; it cannot be selected by model output.
    request_path = Path(args.request).absolute()
    if request_path.name != "request.json" or request_path.parent.parent.name != "runs" or request_path.parent.parent.parent.parent.name != "classroom":
        raise ValueError("Нужен назначенный запрос учебного запуска")
    request = json.loads(_read(request_path, SOURCE_ROOT if request_path.is_relative_to(SOURCE_ROOT) else request_path.parent, MAX_RESULT_BYTES))
    session = Session.open(request["session_id"], root=request["root"])
    expected = session.directory / "runs" / request["run_id"]
    result_path = expected / "result.json"
    if request_path != expected / "request.json" or Path(args.result).absolute() != result_path:
        raise ValueError("Worker принимает только назначенные файлы протокола")
    state = session._state()
    if not state["active_run"] or state["active_run"]["id"] != request["run_id"] or state["owner_pid"] != os.getppid():
        raise ValueError("Нет живого запуска, назначенного родительским терминалом")
    trace = JournalTrace(session, request["run_id"])
    result = {"run_id": request["run_id"], "kind": request["kind"], "status": "error", "cleanup": "confirmed"}
    signal.signal(signal.SIGTERM, _cancel)
    signal.signal(signal.SIGINT, _cancel)
    try:
        parent_signal = _parent_death_signal(state["owner_pid"])
        if any(item["target_run_id"] == request["run_id"] for item in state["control_requests"]):
            raise Cancelled("Остановка запрошена до выполнения")
        trace.emit("worker_phase", phase="started", parent_death_signal=parent_signal)
        status, output = _execute(request, session, trace)
        result.update(status=status, result=output)
        if status == "outcome_unknown":
            result.update(action_outcome="outcome_unknown", cleanup="cleanup_unconfirmed")
        trace.emit("worker_phase", phase="completed", status=status)
    except Cancelled:
        unknown = request["kind"] == "bash" or _pending(request, session)
        result.update(status="cancelled", action_outcome="outcome_unknown" if unknown else "no_pending_side_effect", scope="owned_local_worker")
        try:
            trace.emit("worker_phase", phase="cancelled", action_outcome=result["action_outcome"])
        except Exception:
            pass
    except BaseException as error:
        unknown = getattr(error, "outcome_unknown", False) or _pending(request, session)
        result.update(status="outcome_unknown" if unknown else "error", error_type=type(error).__name__, error=str(error)[:4096])
        if unknown:
            result["action_outcome"] = "outcome_unknown"
        if _cancelling or unknown:
            result["cleanup"] = "cleanup_unconfirmed"
        try:
            trace.emit("worker_phase", phase="error", status=result["status"], error_type=type(error).__name__)
        except Exception:
            pass
    _atomic(result_path, result, session.directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
