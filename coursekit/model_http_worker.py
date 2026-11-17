"""Доверенный одноразовый HTTP worker с ограничением ответа. Не исполняет ответ."""

import json
import sys
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise ValueError("Перенаправление endpoint запрещено")


def main():
    data = json.loads(sys.stdin.buffer.read(1024 * 1024))
    request = urllib.request.Request(
        data["endpoint"] + "/completion",
        data=json.dumps(data["payload"], ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "coursekit/0.1"},
        method="POST",
    )
    try:
        # Не использовать настройки HTTP proxy из пользовательского окружения.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=data["timeout"]) as response:
            body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise ValueError("Ответ сервера превышает 2 MiB")
        sys.stdout.buffer.write(body)
        return 0
    except urllib.error.HTTPError as error:
        print(f"Ошибка HTTP {error.code}; автоматического повтора нет", file=sys.stderr)
    except (ValueError, OSError, urllib.error.URLError) as error:
        print(f"Ошибка HTTP worker: {type(error).__name__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
