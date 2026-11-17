#!/usr/bin/env python3
"""Measure the same held-out task before/after LoRA; --help and --self-test need only stdlib."""

import argparse
import hashlib
import importlib.metadata
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from make_training_data import MODEL_ID, MODEL_REVISION, canonical, load_bundle, score_text, self_test, sha256


def assert_comparable(previous, settings):
    if previous["settings"] != settings:
        raise ValueError("Настройки/данные отличаются от исходного отчёта; изменение качества нельзя приписать адаптеру")
    if previous.get("adapter") is not None:
        raise ValueError("Исходный отчёт должен быть получен без адаптера")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--data", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--compare", type=Path, help="Проверить сопоставимость и вычислить разницу с исходным отчётом")
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument("--revision", default=MODEL_REVISION)
    parser.add_argument("--cache-dir", type=Path, default=Path("work/model-cache"))
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--max-new-tokens", type=int, default=128)
    parser.add_argument("--limit", type=int, default=0, help="0 — все отложенные примеры; одинаково в обоих запусках")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.self_test:
        self_test()
        reference = {"settings": {"model": "fixed", "device": "cpu", "example_ids": ["eval-0"]}, "adapter": None}
        assert_comparable(reference, reference["settings"])
        for changed in [{**reference["settings"], "device": "cuda"}, {**reference["settings"], "example_ids": ["eval-1"]}]:
            try:
                assert_comparable(reference, changed)
            except ValueError:
                pass
            else:
                raise AssertionError("Изменение условий не обнаружено")
        print("PASS: смена устройства или проверочных записей блокирует причинное сравнение")
        return
    if not args.data or not args.output:
        parser.error("Нужны --data и --output")
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision) or min(args.threads, args.max_new_tokens) < 1 or args.limit < 0:
        parser.error("Нужны полный SHA, положительные лимиты и limit >= 0")
    if args.output.exists():
        parser.error("Отчёт уже существует; выберите новый output, чтобы сохранить исходное измерение")
    _, evaluation, data_manifest = load_bundle(args.data)
    if args.limit:
        evaluation = evaluation[:args.limit]
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, set_seed
    except ImportError as exc:
        raise SystemExit("Нужны необязательные зависимости requirements-training.txt в отдельном окружении. Установка не выполнялась.") from exc
    if args.device == "cuda" and not torch.cuda.is_available():
        raise SystemExit("Выбрана cuda, но GPU недоступен. Выберите режим явно.")
    torch.set_num_threads(args.threads)
    set_seed(args.seed)
    load_args = dict(revision=args.revision, cache_dir=str(args.cache_dir),
                     local_files_only=not args.allow_download, token=False, trust_remote_code=False)
    tokenizer = AutoTokenizer.from_pretrained(args.model, **load_args)
    model = AutoModelForCausalLM.from_pretrained(args.model, torch_dtype=torch.float32,
                                               use_safetensors=True, **load_args)
    adapter_manifest = None
    if args.adapter:
        from peft import PeftModel
        adapter_manifest = json.loads((args.adapter / "training-manifest.json").read_text(encoding="utf-8"))
        if (adapter_manifest["base_model"] != args.model or adapter_manifest["base_revision"] != args.revision
                or adapter_manifest["data_sha256"] != data_manifest["sha256"]):
            raise SystemExit("Адаптер обучен на другом основании или наборе данных; сравнение прекращено")
        if sha256(args.adapter / "adapter_model.safetensors") != adapter_manifest["adapter_weights_sha256"]:
            raise SystemExit("Файл весов адаптера изменился после обучения")
        model = PeftModel.from_pretrained(model, args.adapter, is_trainable=False)
    model.to(args.device).eval()
    settings = {
        "model": args.model, "revision": args.revision, "dtype": "float32", "device": args.device,
        "threads": args.threads, "seed": args.seed, "do_sample": False, "max_new_tokens": args.max_new_tokens,
        "eval_sha256": data_manifest["sha256"]["eval.jsonl"],
        "schema_sha256": data_manifest["sha256"]["command.schema.json"],
        "example_ids": [r["id"] for r in evaluation],
        "chat_template_sha256": hashlib.sha256((tokenizer.chat_template or "").encode()).hexdigest(),
        "versions": {name: importlib.metadata.version(name) for name in ["torch", "transformers", "peft"]},
    }
    previous = None
    if args.compare:
        previous = json.loads(args.compare.read_text(encoding="utf-8"))
        try:
            assert_comparable(previous, settings)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    rows = []
    start = time.monotonic()
    with torch.inference_mode():
        for row in evaluation:
            prompt = tokenizer.apply_chat_template(row["messages"], tokenize=False, add_generation_prompt=True)
            inputs = tokenizer(prompt, return_tensors="pt", add_special_tokens=False).to(args.device)
            generated = model.generate(**inputs, max_new_tokens=args.max_new_tokens, do_sample=False,
                                       pad_token_id=tokenizer.eos_token_id)
            new_tokens = generated[0, inputs["input_ids"].shape[-1]:]
            text = tokenizer.decode(new_tokens, skip_special_tokens=True)
            rows.append({"id": row["id"], "action": row["target"]["action"], "prompt": prompt,
                         "expected": row["target"], "output": text, "generated_tokens": len(new_tokens),
                         **score_text(text, row["target"])})
            print(f"{row['id']}: {canonical({key: rows[-1][key] for key in ['valid_json', 'schema_valid', 'semantic_correct']})}", flush=True)
    count = len(rows)
    metrics = {key: {"count": sum(row[key] for row in rows), "total": count,
                     "rate": sum(row[key] for row in rows) / count}
               for key in ["valid_json", "schema_valid", "semantic_correct"]}
    report = {"format": "course-evaluation-v1", "created_at": datetime.now(timezone.utc).isoformat(),
              "settings": settings, "adapter": str(args.adapter) if args.adapter else None,
              "metrics": metrics, "seconds": round(time.monotonic() - start, 3), "examples": rows,
              "adapter_digest": adapter_manifest["adapter_digest_after"] if adapter_manifest else None}
    if previous:
        report["delta"] = {key: metrics[key]["rate"] - previous["metrics"][key]["rate"] for key in metrics}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"metrics": metrics, "delta": report.get("delta"), "output": str(args.output)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
