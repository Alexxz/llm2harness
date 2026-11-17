#!/usr/bin/env python3
"""Train a real LoRA adapter; optional torch/transformers imports happen after --help."""

import argparse
import hashlib
import importlib.metadata
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from make_training_data import MODEL_ID, MODEL_REVISION, canonical, load_bundle, sha256


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Новый каталог адаптера")
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument("--revision", default=MODEL_REVISION, help="Полный commit SHA исходных весов")
    parser.add_argument("--cache-dir", type=Path, default=Path("work/model-cache"))
    parser.add_argument("--allow-download", action="store_true", help="Явно разрешить загрузку публичных весов в cache-dir")
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--max-steps", type=int, default=48)
    parser.add_argument("--max-seconds", type=int, default=1800, help="Мягкая остановка между шагами; не прерывает текущий шаг")
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--gradient-accumulation", type=int, default=4)
    parser.add_argument("--learning-rate", type=float, default=0.0002)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if (not re.fullmatch(r"[0-9a-f]{40}", args.revision)
            or min(args.threads, args.max_steps, args.max_seconds, args.max_length, args.batch_size, args.gradient_accumulation) < 1
            or args.learning_rate <= 0):
        parser.error("Нужны полный SHA и положительные ресурсные параметры")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("Каталог output должен быть новым или пустым")
    return args


def main():
    args = parse_args()
    train, _, data_manifest = load_bundle(args.data)
    try:
        import torch
        from peft import LoraConfig, TaskType, get_peft_model
        from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainerCallback, TrainingArguments, set_seed
    except ImportError as exc:
        raise SystemExit("Нужны необязательные зависимости requirements-training.txt в отдельном окружении. Установка не выполнялась.") from exc
    if args.device == "cuda" and not torch.cuda.is_available():
        raise SystemExit("Выбрана cuda, но GPU недоступен. Автоматической смены режима нет.")
    torch.set_num_threads(args.threads)
    set_seed(args.seed)
    load_args = dict(revision=args.revision, cache_dir=str(args.cache_dir),
                     local_files_only=not args.allow_download, token=False, trust_remote_code=False)
    tokenizer = AutoTokenizer.from_pretrained(args.model, **load_args)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    base = AutoModelForCausalLM.from_pretrained(args.model, torch_dtype=torch.float32,
                                              use_safetensors=True, **load_args)
    base.config.use_cache = False
    config = LoraConfig(task_type=TaskType.CAUSAL_LM, r=8, lora_alpha=16,
                        lora_dropout=0.0, target_modules=["q_proj", "v_proj"], bias="none")
    model = get_peft_model(base, config)
    model.to(args.device)

    def parameter_digest():
        digest = hashlib.sha256()
        for name, parameter in model.named_parameters():
            if parameter.requires_grad:
                digest.update(name.encode())
                digest.update(parameter.detach().cpu().float().numpy().tobytes())
        return digest.hexdigest()

    encoded = []
    for row in train:
        prompt = tokenizer.apply_chat_template(row["messages"], tokenize=True, add_generation_prompt=True)
        full = tokenizer.apply_chat_template(row["messages"] + [{"role": "assistant", "content": canonical(row["target"])}],
                                             tokenize=True, add_generation_prompt=False)
        if full[:len(prompt)] != prompt:
            raise SystemExit("Шаблон чата не даёт ожидаемый префикс: маска ответа была бы неверной")
        if len(full) > args.max_length:
            raise SystemExit(f"Пример {row['id']} длиннее max-length; увеличьте лимит, не обрезайте ответ молча")
        encoded.append({"input_ids": full, "attention_mask": [1] * len(full),
                        "labels": [-100] * len(prompt) + full[len(prompt):]})

    class Examples(torch.utils.data.Dataset):
        def __len__(self):
            return len(encoded)

        def __getitem__(self, index):
            return encoded[index]

    def collate(rows):
        length = max(len(row["input_ids"]) for row in rows)
        output = {}
        for key, pad in [("input_ids", tokenizer.pad_token_id), ("attention_mask", 0), ("labels", -100)]:
            output[key] = torch.tensor([row[key] + [pad] * (length - len(row[key])) for row in rows], dtype=torch.long)
        return output

    class TimeLimit(TrainerCallback):
        def __init__(self):
            self.start = None
            self.stopped = False

        def on_train_begin(self, args, state, control, **kwargs):
            self.start = time.monotonic()

        def on_step_end(self, args, state, control, **kwargs):
            if time.monotonic() - self.start >= args_course.max_seconds:
                self.stopped = True
                control.should_training_stop = True
            return control

    args_course = args
    limiter = TimeLimit()
    args.output.mkdir(parents=True, exist_ok=True)
    before = parameter_digest()
    trainer = Trainer(
        model=model,
        args=TrainingArguments(
            output_dir=str(args.output / "trainer"), use_cpu=args.device == "cpu",
            max_steps=args.max_steps, per_device_train_batch_size=args.batch_size,
            gradient_accumulation_steps=args.gradient_accumulation, learning_rate=args.learning_rate,
            logging_steps=1, save_strategy="no", eval_strategy="no", report_to="none", push_to_hub=False,
            seed=args.seed, data_seed=args.seed, bf16=False, fp16=False, dataloader_num_workers=0,
            dataloader_pin_memory=False, optim="adamw_torch", label_names=["labels"],
        ),
        train_dataset=Examples(), data_collator=collate, processing_class=tokenizer, callbacks=[limiter],
    )
    start = time.monotonic()
    result = trainer.train()
    after = parameter_digest()
    if before == after:
        raise SystemExit("Обучаемые параметры не изменились; успешное дообучение не подтверждено")
    model.save_pretrained(args.output, safe_serialization=True)
    tokenizer.save_pretrained(args.output)
    manifest = {
        "format": "course-lora-v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "base_model": args.model, "base_revision": args.revision,
        "data_sha256": data_manifest["sha256"], "seed": args.seed,
        "device": args.device, "dtype": "float32", "max_length": args.max_length,
        "batch_size": args.batch_size, "gradient_accumulation": args.gradient_accumulation,
        "requested_steps": args.max_steps, "completed_steps": trainer.state.global_step,
        "max_seconds_soft_limit": args.max_seconds, "stopped_by_time_limit": limiter.stopped,
        "seconds": round(time.monotonic() - start, 3), "learning_rate": args.learning_rate,
        "trainable_parameter_count": sum(p.numel() for p in model.parameters() if p.requires_grad),
        "adapter_digest_before": before, "adapter_digest_after": after,
        "adapter_weights_sha256": sha256(args.output / "adapter_model.safetensors"),
        "training_metrics": result.metrics, "evaluation_performed": False,
        "versions": {name: importlib.metadata.version(name) for name in ["torch", "transformers", "peft", "accelerate"]},
    }
    (args.output / "training-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
