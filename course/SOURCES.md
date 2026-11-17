# Первичные источники курса

Материалы опираются на документацию самих инструментов. Для практики фиксируются конкретные версии и настройки; ссылка на текущую документацию не заменяет проверку поведения подготовленной версии.

## Ядро и управление генерацией

- [llama.cpp](https://github.com/ggml-org/llama.cpp) — сервер ядра, параметры генерации и грамматики.
- [Релиз b12380](https://github.com/ggml-org/llama.cpp/releases/tag/b12380) — подготовленный в текущем окружении runtime.
- [Qwen2.5 1.5B](https://huggingface.co/Qwen/Qwen2.5-1.5B) — исходная базовая модель.
- [Qwen2.5 1.5B Instruct GGUF](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF) — версия для инструкций в формате локального runtime.
- [JSON Schema](https://json-schema.org/understanding-json-schema) — структура и границы проверки данных.

## Обучение

- [Hugging Face PEFT](https://huggingface.co/docs/peft/index) — подключаемые обучаемые адаптеры.
- [Transformers Trainer](https://huggingface.co/docs/transformers/main_classes/trainer) — цикл настоящего обучения.
- [Qwen2.5 0.5B Instruct](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct) — маленькая модель учебного эксперимента. Точная ревизия зафиксирована в скриптах.

Версионные ссылки и параметры конкретного эксперимента приведены в занятиях 04–06. Обучение не объявляется выполненным только на основании документации API.

## Агент и изоляция

- [OpenAI о harness Codex](https://developers.openai.com/blog/codex-as-a-platform) — разделение модели и окружающей системы исполнения.
- [Docker run](https://docs.docker.com/reference/cli/docker/container/run/) — изоляция, пользователи, файловые системы и лимиты.
- [GNU Bash](https://www.gnu.org/software/bash/manual/bash.html) — интерфейсы учебных программ и терминальной игры.

## MCP и спецификации

- [MCP stdio transport 2025 06 18](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports).
- [MCP lifecycle 2025 06 18](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle).
- [MCP tools 2025 06 18](https://modelcontextprotocol.io/specification/2025-06-18/server/tools).
- [OpenSpec](https://github.com/Fission-AI/OpenSpec) и [процессы работы](https://github.com/Fission-AI/OpenSpec/blob/main/docs/workflows.md).

Учебная инструкция [GrillMe](../skills/grill-me.md) содержит атрибуцию исходного локального навыка и явные правила адаптации для собственного harness.
