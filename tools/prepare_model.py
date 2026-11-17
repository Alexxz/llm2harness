"""Явная подготовка одного фиксированного учебного GGUF. Без --download только план."""

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=("base", "instruct"), required=True)
    parser.add_argument("--download", action="store_true", help="Явно разрешить загрузку выбранного файла весов")
    args = parser.parse_args()
    profiles = json.loads((ROOT / "resources/models.json").read_text(encoding="utf-8"))
    profile = profiles[args.variant]
    destination = ROOT / "models" / profile["local_name"]
    url = f"https://huggingface.co/{profile['repository']}/resolve/{profile['revision']}/{profile['filename']}"
    if destination.is_symlink() or destination.parent.is_symlink():
        parser.error("Символические ссылки в каталоге весов не разрешены")
    if not args.download:
        print(json.dumps({"download": False, "url": url, "destination": str(destination), **profile}, ensure_ascii=False, indent=2))
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.stat().st_size == profile["bytes"] and digest(destination) == profile["sha256"]:
            print("Веса уже подготовлены; SHA256 совпадает")
            return 0
        parser.error("Существующий файл не соответствует профилю; он сохранён без изменений")
    partial = destination.with_suffix(destination.suffix + ".part")
    if partial.is_symlink():
        parser.error("Временный файл не может быть ссылкой")
    if partial.exists():
        parser.error("Незавершённая загрузка уже существует; она сохранена. Выберите дальнейшее действие вручную")
    request = urllib.request.Request(url, headers={"User-Agent": "coursekit/0.1"})
    written = 0
    checksum = hashlib.sha256()
    # Пишем только новый временный файл. Отмена не уничтожает готовые веса.
    with urllib.request.urlopen(request, timeout=30) as response, partial.open("xb") as target:
        while chunk := response.read(1024 * 1024):
            written += len(chunk)
            if written > profile["bytes"]:
                raise ValueError("Сервер прислал больше ожидаемого размера; файл оставлен временным")
            target.write(chunk)
            checksum.update(chunk)
            print(f"\r{written / 1048576:.0f} / {profile['bytes'] / 1048576:.0f} MiB", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    if written != profile["bytes"] or checksum.hexdigest() != profile["sha256"]:
        raise ValueError("Размер или SHA256 не совпал; файл не объявлен готовыми весами")
    partial.rename(destination)
    print(json.dumps({"ready": str(destination), "sha256": profile["sha256"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
