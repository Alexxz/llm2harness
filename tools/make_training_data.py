#!/usr/bin/env python3
"""Create/check the synthetic SFT dataset. This module needs only stdlib."""

import argparse
import hashlib
import json
import random
from pathlib import Path

MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"
MODEL_REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"
SYSTEM = (
    "Преобразуй запрос в один объект JSON без пояснений и Markdown. "
    "Для чтения: action=read_file, arguments содержит только path. "
    "Для записи: action=write_file, arguments содержит только path и content. "
    "Для завершения: action=finish, arguments содержит только answer. "
    "Сохраняй имена файлов и текст из запроса точно. Никакие действия не выполняй."
)
SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "oneOf": [
        {"type": "object", "required": ["action", "arguments"], "additionalProperties": False,
         "properties": {"action": {"const": "read_file"}, "arguments": {
             "type": "object", "required": ["path"], "additionalProperties": False,
             "properties": {"path": {"type": "string", "minLength": 1}}}}},
        {"type": "object", "required": ["action", "arguments"], "additionalProperties": False,
         "properties": {"action": {"const": "write_file"}, "arguments": {
             "type": "object", "required": ["path", "content"], "additionalProperties": False,
             "properties": {"path": {"type": "string", "minLength": 1}, "content": {"type": "string"}}}}},
        {"type": "object", "required": ["action", "arguments"], "additionalProperties": False,
         "properties": {"action": {"const": "finish"}, "arguments": {
             "type": "object", "required": ["answer"], "additionalProperties": False,
             "properties": {"answer": {"type": "string"}}}}},
    ],
}
TEMPLATES = {
    "train": {
        "read_file": ["Прочитай файл «{path}».", "Покажи содержимое файла «{path}»."],
        "write_file": ["Запиши в файл «{path}» текст «{content}».", "Создай файл «{path}» с содержимым «{content}»."],
        "finish": ["Заверши задачу сообщением «{content}».", "Закончи работу с ответом «{content}»."],
    },
    "eval": {
        "read_file": ["Мне нужно увидеть текст из «{path}»: запроси чтение этого файла.", "Выполни чтение документа с именем «{path}»."],
        "write_file": ["Пусть файл «{path}» содержит ровно «{content}»: предложи запись.", "Сохрани строку «{content}» в документе «{path}»."],
        "finish": ["Работа завершена. Итоговый ответ должен быть ровно «{content}».", "Больше действий не требуется; верни окончательное сообщение «{content}»."],
    },
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_target(target):
    """Validate our complete small action contract, without a dependency."""
    if not isinstance(target, dict) or set(target) != {"action", "arguments"}:
        return False
    fields = {"read_file": {"path"}, "write_file": {"path", "content"}, "finish": {"answer"}}
    arguments = target["arguments"]
    if not isinstance(target["action"], str) or target["action"] not in fields or not isinstance(arguments, dict):
        return False
    if set(arguments) != fields[target["action"]] or not all(isinstance(v, str) for v in arguments.values()):
        return False
    return "path" not in arguments or bool(arguments["path"])


def score_text(text, expected):
    """No cleanup of fences or prose: measure what the model actually returned."""
    def reject_constant(value):
        raise ValueError(f"Нестандартная константа JSON: {value}")

    try:
        value = json.loads(text, parse_constant=reject_constant)
    except (ValueError, TypeError):
        return {"valid_json": False, "schema_valid": False, "semantic_correct": False}
    valid = validate_target(value)
    return {"valid_json": True, "schema_valid": valid,
            "semantic_correct": valid and value == expected}


def generate_rows(train_size=240, eval_size=60, seed=42):
    if train_size < 3 or eval_size < 3:
        raise ValueError("Каждая выборка должна содержать минимум три примера")
    rng = random.Random(seed)
    entities = set()
    result = {}
    actions = list(TEMPLATES["train"])
    for split, size in [("train", train_size), ("eval", eval_size)]:
        rows = []
        for index in range(size):
            action = actions[index % len(actions)]
            while True:
                entity = f"{rng.getrandbits(64):016x}"
                if entity not in entities:
                    entities.add(entity)
                    break
            path = f"note-{entity}.txt"
            content = f"Маяк {entity} готов"
            template_index = rng.randrange(len(TEMPLATES[split][action]))
            user = TEMPLATES[split][action][template_index].format(path=path, content=content)
            arguments = ({"path": path} if action == "read_file" else
                         {"path": path, "content": content} if action == "write_file" else
                         {"answer": content})
            rows.append({
                "id": f"{split}-{index:04d}", "split": split, "entity_id": entity,
                "template_id": f"{split}-{action}-{template_index}",
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                "target": {"action": action, "arguments": arguments},
            })
        rng.shuffle(rows)
        result[split] = rows
    validate_rows(result["train"], result["eval"])
    return result


def validate_rows(train, evaluation):
    for split, rows in [("train", train), ("eval", evaluation)]:
        if not rows:
            raise ValueError(f"Пустая выборка {split}")
        for row in rows:
            if row.get("split") != split or not validate_target(row.get("target")):
                raise ValueError(f"Неверная запись {row.get('id')}")
            messages = row.get("messages")
            if (not isinstance(messages, list) or len(messages) != 2
                    or [m.get("role") for m in messages] != ["system", "user"]
                    or not all(isinstance(m.get("content"), str) and m["content"] for m in messages)):
                raise ValueError(f"Неверные messages у {row.get('id')}")
            if any(canonical(row["target"]) in message["content"] for message in messages):
                raise ValueError("Готовый целевой JSON оказался в промпте")
    for field in ["id", "entity_id", "template_id"]:
        left, right = {r[field] for r in train}, {r[field] for r in evaluation}
        if left & right:
            raise ValueError(f"Утечка между выборками: совпал {field}")
    prompts = lambda rows: {canonical(r["messages"]) for r in rows}
    targets = lambda rows: {canonical(r["target"]) for r in rows}
    if prompts(train) & prompts(evaluation) or targets(train) & targets(evaluation):
        raise ValueError("Утечка одинаковых входов или ответов между выборками")
    ids = [r["id"] for r in train + evaluation]
    if len(ids) != len(set(ids)):
        raise ValueError("Повторяющиеся идентификаторы")


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def load_bundle(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for name in ["train.jsonl", "eval.jsonl", "command.schema.json"]:
        if sha256(directory / name) != manifest["sha256"].get(name):
            raise ValueError(f"Данные изменились после фиксации: {name}")
    schema = json.loads((directory / "command.schema.json").read_text(encoding="utf-8"))
    if schema != SCHEMA:
        raise ValueError("Схема отличается от контракта курса")
    train, evaluation = read_jsonl(directory / "train.jsonl"), read_jsonl(directory / "eval.jsonl")
    validate_rows(train, evaluation)
    return train, evaluation, manifest


def write_bundle(directory, train_size=240, eval_size=60, seed=42):
    directory = Path(directory)
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("Каталог уже содержит файлы; выберите новый каталог")
    directory.mkdir(parents=True, exist_ok=True)
    data = generate_rows(train_size, eval_size, seed)
    for split, rows in data.items():
        with (directory / f"{split}.jsonl").open("x", encoding="utf-8") as stream:
            for row in rows:
                stream.write(canonical(row) + "\n")
    (directory / "command.schema.json").write_text(json.dumps(SCHEMA, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "format": "course-sft-v1", "seed": seed, "train_size": train_size, "eval_size": eval_size,
        "source": "synthetic_generated_inputs_not_model_outputs", "model": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "split_policy": "disjoint entities, templates, inputs and targets; eval is never sent to Trainer",
        "sha256": {name: sha256(directory / name) for name in ["train.jsonl", "eval.jsonl", "command.schema.json"]},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    load_bundle(directory)
    return manifest


def self_test():
    data = generate_rows(30, 12, 42)
    assert data == generate_rows(30, 12, 42)
    target = data["eval"][0]["target"]
    assert score_text(canonical(target), target)["semantic_correct"]
    assert not score_text("```json\n" + canonical(target) + "\n```", target)["valid_json"]
    assert not score_text('{"action":"finish","arguments":{"answer":"другой ответ"}}', target)["semantic_correct"]
    assert not score_text('{"action":"read_file","arguments":{"path":17}}', target)["schema_valid"]
    assert not score_text('{"action":[],"arguments":{}}', target)["schema_valid"]
    assert not score_text('NaN', target)["valid_json"]
    contaminated = [dict(row) for row in data["eval"]]
    contaminated[0]["entity_id"] = data["train"][0]["entity_id"]
    try:
        validate_rows(data["train"], contaminated)
    except ValueError:
        pass
    else:
        raise AssertionError("Проверка не обнаружила утечку сущности")
    print("PASS: детерминированные данные, раздельные выборки, обнаружение утечки, метрики формата и смысла")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path, help="Новый каталог train/eval/schema/manifest")
    mode.add_argument("--check", type=Path, help="Проверить существующий набор без изменения")
    mode.add_argument("--self-test", action="store_true", help="Проверить генератор и метрики без модели")
    parser.add_argument("--train-size", type=int, default=240)
    parser.add_argument("--eval-size", type=int, default=60)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    try:
        if args.self_test:
            self_test()
        elif args.check:
            _, _, manifest = load_bundle(args.check)
            print(json.dumps({"status": "valid", "manifest": manifest}, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(write_bundle(args.output, args.train_size, args.eval_size, args.seed), ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        parser.exit(2, f"Ошибка данных: {exc}\n")


if __name__ == "__main__":
    main()
