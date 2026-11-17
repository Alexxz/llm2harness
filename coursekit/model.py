"""Один HTTP запрос к ядру llama.cpp. Здесь нет цикла агента и исполнения кода."""

import hashlib
import ipaddress
import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path


class ModelError(RuntimeError):
    pass


class ModelClient:
    def __init__(self, endpoint="http://127.0.0.1:8088", *, allow_remote=False, timeout=60, trace=None):
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Endpoint должен быть адресом HTTP или HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("Укажите только origin сервера, без credentials, пути, query и fragment")
        try:
            local = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            local = parsed.hostname.lower() == "localhost"
        if not local and not allow_remote:
            raise ValueError("Для внешнего сервера требуется явный --allow-remote")
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
            raise ValueError("Timeout должен быть больше 0 и не больше 300 секунд")
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.trace = trace

    def generate(self, prompt, *, max_tokens=256, temperature=0.0, seed=42, json_schema=None):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Промпт должен быть непустым текстом")
        if len(prompt.encode("utf-8")) > 262144:
            raise ValueError("Промпт превышает учебный предел 256 KiB")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 4096:
            raise ValueError("max_tokens: целое число от 1 до 4096")
        if not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2:
            raise ValueError("temperature: число от 0 до 2")
        payload = {
            "prompt": prompt,
            "n_predict": max_tokens,
            "temperature": temperature,
            "seed": seed,
            "stream": False,
            "cache_prompt": False,
        }
        if json_schema is not None:
            if not isinstance(json_schema, dict):
                raise ValueError("JSON Schema должна быть объектом")
            payload["json_schema"] = json_schema
        if self.trace:
            self.trace.emit("model_request", endpoint=self.endpoint, request=payload)
        started = time.monotonic()
        try:
            # Отдельный доверенный HTTP worker позволяет остановить весь запрос,
            # даже если сервер медленно присылает байты и не достигает socket timeout.
            worker = subprocess.run(
                [sys.executable, "-I", str(Path(__file__).with_name("model_http_worker.py"))],
                input=json.dumps({"endpoint": self.endpoint, "timeout": self.timeout, "payload": payload}, ensure_ascii=False),
                text=True, encoding="utf-8", capture_output=True, timeout=self.timeout, check=False,
            )
            if worker.returncode:
                raise ModelError(worker.stderr.strip()[:500] or "HTTP worker завершился с ошибкой")
            decoded = json.loads(worker.stdout)
            if not isinstance(decoded, dict) or not isinstance(decoded.get("content"), str):
                raise ModelError("Сервер должен вернуть объект с текстовым полем content")
        except subprocess.TimeoutExpired as error:
            raise ModelError("Общий таймаут запроса: HTTP worker остановлен; автоматического повтора нет") from error
        except (TimeoutError, OSError) as error:
            raise ModelError(f"Ошибка транспорта ({type(error).__name__}); автоматического повтора нет") from error
        except (ValueError, UnicodeDecodeError) as error:
            raise ModelError("Сервер вернул неправильный JSON") from error
        result = {
            "text": decoded["content"],
            "metadata": {
                "endpoint": self.endpoint,
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "settings": {key: value for key, value in payload.items() if key != "prompt"},
                "server": {key: decoded[key] for key in ("model", "timings", "tokens_predicted", "tokens_evaluated", "truncated", "stopped_eos", "stopped_limit", "stop_type") if key in decoded},
            },
        }
        if self.trace:
            self.trace.emit("model_response", **result)
        return result
