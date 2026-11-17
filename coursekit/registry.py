"""Проверка полноты учебных материалов. Не оценивает способность модели."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECTIONS = (
    "Цель", "Перед началом", "План занятия", "Опыты", "Ожидаемые наблюдения",
    "Если результат отличается", "Что сохранить", "Критерии завершения", "Свой эксперимент", "Источники",
)


def catalog():
    lessons = []
    for path in sorted((ROOT / "lessons").glob("*/lesson.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        item["directory"] = str(path.parent.relative_to(ROOT))
        lessons.append(item)
    return lessons


def lesson(identifier):
    normalized = str(identifier).zfill(2)
    matches = [item for item in catalog() if item["id"] == normalized]
    if len(matches) != 1:
        raise ValueError(f"Не найдено ровно одно занятие {normalized}")
    return matches[0]


def asset_path(directory, value):
    directory = Path(directory).resolve()
    path = directory / value
    if Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError(f"Путь выходит из каталога занятия: {value}")
    if path.is_symlink() or not path.resolve().is_relative_to(directory):
        raise ValueError(f"Небезопасный путь актива: {value}")
    if not path.is_file():
        raise ValueError(f"Отсутствует файл: {path}")
    return path


def validate_delivery_contract(directory, item):
    """Check executable entry assets; semantic teaching quality still needs a pilot."""
    if item.get("delivery_contract") != "interactive-v1":
        return []
    errors = []
    names = {"TEACHER_START.md": 8192, "EXPLANATIONS.md": 65536}
    if "AFTER_SHOW.md" in item.get("artifacts", []):
        names["AFTER_SHOW.md"] = 8192
    for name, maximum in names.items():
        try:
            path = asset_path(directory, name)
            data = path.read_bytes()
            if not data.strip():
                errors.append(f"{item.get('id')}: пустой {name}")
            elif len(data) > maximum:
                errors.append(f"{item.get('id')}: {name} превышает лимит runtime {maximum} байт")
        except (ValueError, OSError) as error:
            errors.append(str(error))
    return errors


def validate_course():
    from jsonschema import Draft202012Validator

    errors = []
    try:
        items = catalog()
    except (ValueError, OSError) as error:
        return [str(error)]
    identifiers = [item.get("id") for item in items]
    expected = [f"{index:02}" for index in range(19)]
    if identifiers != expected:
        errors.append(f"Ожидается вводное занятие 00 и основные 01–18 ровно по одному; получены {identifiers}")
    schema = json.loads((ROOT / "schemas" / "lesson.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    for item in items:
        directory = ROOT / item["directory"]
        errors.extend(validate_delivery_contract(directory, item))
        clean = {key: value for key, value in item.items() if key != "directory"}
        for error in validator.iter_errors(clean):
            errors.append(f"{item.get('id', '?')}: {'/'.join(str(part) for part in error.path)}: {error.message}")
        for parent in item.get("depends_on", []):
            if parent not in expected or parent >= item.get("id", ""):
                errors.append(f"{item.get('id')}: некорректная зависимость {parent}")
        for name in ("README.md", "INTRO.md", "INSTRUCTOR.md", "prompts/baseline.txt", "prompts/variation.txt", *item.get("artifacts", [])):
            try:
                asset_path(directory, name)
            except ValueError as error:
                errors.append(str(error))
        readme = directory / "README.md"
        if readme.is_file():
            text = readme.read_text(encoding="utf-8")
            headings = set(re.findall(r"^## (.+)$", text, flags=re.MULTILINE))
            for heading in SECTIONS:
                if heading not in headings:
                    errors.append(f"{item.get('id')}: нет раздела {heading}")
        experiment_ids = []
        for experiment in item.get("experiments", []):
            experiment_ids.append(experiment.get("id"))
            for key in ("prompt_file", "fixture_file"):
                if key in experiment:
                    try:
                        asset_path(directory, experiment[key])
                    except ValueError as error:
                        errors.append(str(error))
        if len(experiment_ids) != len(set(experiment_ids)):
            errors.append(f"{item.get('id')}: повтор id опыта")
    return errors
