"""Проверить протокол диалогов и подготовить обезличенный пакет проверяющему.

Не оценивает качество преподавания и не обращается к модели.
"""

import argparse
import hashlib
import json
from pathlib import Path
import random
import re

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "course/simulations"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def prepare(version):
    if not re.fullmatch(r"v[1-9][0-9]*", version):
        raise ValueError("Ожидается номер версии вида v1")
    scenarios = {item["id"]: item for item in load(BASE / "scenarios.json")["cases"]}
    runs = BASE / "runs" / version
    manifest_path = runs / "source-manifest.json"
    frozen = load(manifest_path).get("sources", {}) if manifest_path.is_file() else {}
    transcripts = []
    errors = []
    expected_paths = [runs / f"{identifier}.json" for identifier in scenarios] + [runs / "negative-control.json"]
    for path in sorted(path for path in expected_paths if path.is_file()):
        item = load(path)
        identifier = item.get("case_id")
        case = scenarios.get(identifier)
        if case is None:
            errors.append(f"{path.name}: неизвестный сценарий")
            continue
        if item.get("evidence_type") != "agent_pedagogy_simulation":
            errors.append(f"{path.name}: неверный вид свидетельства")
        participants = item.get("participants", {})
        if not participants.get("learner") or not participants.get("teacher") or participants.get("learner") == participants.get("teacher"):
            errors.append(f"{path.name}: нужны раздельные роли")
        if len(item.get("rounds", [])) != 3:
            errors.append(f"{path.name}: нужны три вмешательства")
        for index, turn in enumerate(item.get("rounds", []), start=1):
            if not all(isinstance(turn.get(role), str) and turn[role].strip() for role in ("teacher", "student")):
                errors.append(f"{path.name}: неполная пара реплик {index}")
        for stage in ("pretest", "posttest"):
            if not isinstance(item.get(stage, {}).get("student"), str) or not item[stage]["student"].strip():
                errors.append(f"{path.name}: отсутствует ответ {stage}")
        if item.get("pretest", {}).get("question") != case["pre_question"] or item.get("posttest", {}).get("question") != case["transfer_question"]:
            errors.append(f"{path.name}: контрольные вопросы изменены")
        hashes = item.get("source_hashes", {})
        if not hashes:
            errors.append(f"{path.name}: отсутствуют хеши материалов")
        for relative, expected in hashes.items():
            source = ROOT / relative
            if Path(relative).is_absolute() or ".." in Path(relative).parts or not (relative.startswith("lessons/") or relative in {"course/METHODOLOGY.md", "course/TERMINAL_CLASSROOM.md", "course/TEACHER_GUIDE.md"}):
                errors.append(f"{path.name}: путь вне материалов роли")
                continue
            archived = frozen.get(relative, {})
            archived_text = archived.get("text") if isinstance(archived, dict) else None
            archived_valid = isinstance(archived_text, str) and hashlib.sha256(archived_text.encode("utf-8")).hexdigest() == expected and archived.get("sha256") == expected
            current_valid = source.is_file() and not source.is_symlink() and hashlib.sha256(source.read_bytes()).hexdigest() == expected
            if not archived_valid and not current_valid:
                errors.append(f"{path.name}: источник изменён: {relative}")
        transcripts.append((path, item, case))
    if not transcripts:
        errors.append("Нет диалогов")
    if version == "v1":
        actual = {item["case_id"] for _, item, _ in transcripts if item.get("variant") != "negative-control"}
        if actual != set(scenarios):
            errors.append("Не все шесть исходных сценариев завершены")
        if sum(item.get("variant") == "negative-control" for _, item, _ in transcripts) != 1:
            errors.append("Нужна одна отдельная отрицательная контрольная ветка")
    if errors:
        raise ValueError("\n".join(errors))
    random.Random(20261116).shuffle(transcripts)
    packet, mapping = [], {}
    for number, (path, item, case) in enumerate(transcripts, start=1):
        episode = f"episode-{number:02}"
        packet.append({
            "episode": episode,
            "evidence_type": "agent_pedagogy_simulation",
            "lesson": case["lesson"],
            "observations": case["observations"],
            "pretest": item["pretest"],
            "rounds": item["rounds"],
            "posttest": item["posttest"],
            "content_criteria": case["oracle"],
        })
        mapping[episode] = {"source": str(path.relative_to(ROOT)), "case_id": item["case_id"], "variant": item["variant"]}
    output = BASE / "review" / version
    output.mkdir(parents=True, exist_ok=True)
    (output / "blind-packet.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "mapping.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"protocol_valid": True, "episodes": len(packet), "packet": str((output / "blind-packet.json").relative_to(ROOT)), "judgment": "not_performed_by_this_script"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="v1")
    parser.add_argument("--check-judgment", action="store_true", help="Проверить полноту оценок, арифметику рубрики и точные цитаты после независимого ревью")
    args = parser.parse_args()
    try:
        result = check_judgment(args.version) if args.check_judgment else prepare(args.version)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, str(error) + "\n")


def check_judgment(version):
    if not re.fullmatch(r"v[1-9][0-9]*", version):
        raise ValueError("Ожидается номер версии вида v1")
    directory = BASE / "review" / version
    packet = load(directory / "blind-packet.json")
    packet_by_id = {item["episode"]: item for item in packet}
    mapping = load(directory / "mapping.json")
    judgment = load(directory / "judgment.json")
    keys = {"diagnosis", "next_step", "student_agency", "evidence_integrity", "understanding_check", "transfer"}
    errors = []
    episodes = judgment.get("episodes", [])
    expected_ids = {item["episode"] for item in packet}
    actual_ids = [item.get("episode") for item in episodes]
    if set(actual_ids) != expected_ids or len(actual_ids) != len(expected_ids):
        errors.append("Не все эпизоды оценены ровно один раз")
    results = []
    exact_quotes_checked = 0
    for item in episodes:
        identifier = item.get("episode")
        scores = item.get("scores", {})
        if set(scores) != keys or any(type(value) is not int or not 0 <= value <= 2 for value in scores.values()):
            errors.append(f"{identifier}: неверные баллы")
            continue
        evidence = item.get("evidence", {})
        if set(evidence) != keys or any(not isinstance(value, (str, dict)) or not value for value in evidence.values()):
            errors.append(f"{identifier}: нет обоснования каждого балла")
        for criterion in keys:
            explanation = evidence.get(criterion)
            citations = explanation.get("citations", []) if isinstance(explanation, dict) else []
            if not citations:
                errors.append(f"{identifier}/{criterion}: нужны точные цитаты")
        critical = item.get("critical_failures")
        if not isinstance(critical, list):
            errors.append(f"{identifier}: нет списка критических неуспехов")
            continue
        total = sum(scores.values())
        accepted = total >= 9 and not critical and all(scores[key] > 0 for key in ("diagnosis", "next_step", "transfer"))
        if item.get("total") != total or type(item.get("accepted")) is not bool or item["accepted"] != accepted:
            errors.append(f"{identifier}: итог не соответствует зафиксированной рубрике")
        results.append({**mapping.get(identifier, {}), "episode": identifier, "score": total, "accepted": accepted, "critical_failures": critical})

    def check_citations(obj, episode=None):
        nonlocal exact_quotes_checked
        if isinstance(obj, dict):
            if isinstance(obj.get("episode"), str) and obj["episode"] in packet_by_id:
                episode = obj["episode"]
            if "quote" in obj and ("path" in obj or "turn" in obj):
                path = obj.get("path", obj.get("turn"))
                quote = obj["quote"]
                try:
                    if not isinstance(path, str) or not isinstance(quote, str) or not quote:
                        raise ValueError("Неверная ссылка на цитату")
                    absolute = re.fullmatch(r"\$\[(\d+)\]\.(.+)", path)
                    if absolute:
                        value = packet[int(absolute[1])]
                        if episode is not None and value["episode"] != episode:
                            raise ValueError("Цитата из другого эпизода")
                        relative = absolute[2]
                    else:
                        value = packet_by_id[episode]
                        relative = path
                    if not re.fullmatch(r"[^.\[\]]+(?:\[\d+\]|\.[^.\[\]]+)*", relative):
                        raise ValueError("Неверный путь")
                    for name, index in re.findall(r"([^.\[\]]+)|\[(\d+)\]", relative):
                        value = value[int(index)] if index else value[name]
                    if not isinstance(value, str) or quote not in value:
                        raise ValueError("Цитата отсутствует в реплике")
                    exact_quotes_checked += 1
                except (KeyError, IndexError, TypeError, ValueError):
                    errors.append(f"{episode or 'review'}: цитата не совпадает: {path}")
            for child in obj.values():
                check_citations(child, episode)
        elif isinstance(obj, list):
            for child in obj:
                check_citations(child, episode)

    check_citations(judgment)
    if errors:
        raise ValueError("\n".join(errors))
    summary = {
        "evidence_type": "agent_pedagogy_simulation_review",
        "judgment_consistent": True,
        "exact_quotes_checked": exact_quotes_checked,
        "episodes": results,
        "accepted_regular_episodes": sum(item["accepted"] for item in results if item.get("variant") != "negative-control"),
        "regular_episode_count": sum(item.get("variant") != "negative-control" for item in results),
        "negative_control_rejected": all(not item["accepted"] for item in results if item.get("variant") == "negative-control") if any(item.get("variant") == "negative-control" for item in results) else None,
        "human_learning_measured": False,
    }
    (directory / "checked-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    main()
