"""Render explicit teaching transcripts and append a saved real model response."""

import argparse
import json
from pathlib import Path


ROLES = {"system": "Система", "user": "Пользователь", "assistant": "Ассистент"}


def load_messages(path):
    messages = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a nonempty list")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ROLES:
            raise ValueError("each message needs a known role")
        if not isinstance(message.get("content"), str) or not message["content"].strip():
            raise ValueError("each message needs nonempty content")
    return messages


def render(messages, last_only=False):
    if last_only:
        users = [message for message in messages if message["role"] == "user"]
        if not users:
            raise ValueError("last-only requires a user message")
        messages = [message for message in messages if message["role"] == "system"] + users[-1:]
    chunks = [f"{ROLES[message['role']]}:\n{message['content']}" for message in messages]
    chunks.append("Ассистент:")
    return "\n\n".join(chunks) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    renderer = sub.add_parser("render")
    renderer.add_argument("--input", type=Path, required=True)
    renderer.add_argument("--output", type=Path, required=True)
    renderer.add_argument("--last-only", action="store_true")
    appender = sub.add_parser("append")
    appender.add_argument("--messages", type=Path, required=True)
    appender.add_argument("--response", type=Path, required=True)
    appender.add_argument("--question", required=True)
    appender.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "render":
            output = render(load_messages(args.input), args.last_only)
        else:
            messages = load_messages(args.messages)
            if messages[-1]["role"] != "user":
                raise ValueError("the prior messages must end with user")
            response = json.loads(args.response.read_text(encoding="utf-8"))
            text = response.get("text") if isinstance(response, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise ValueError("the saved response needs nonempty text")
            if not args.question.strip():
                raise ValueError("question must be nonempty")
            messages += [{"role": "assistant", "content": text}, {"role": "user", "content": args.question}]
            output = json.dumps(messages, ensure_ascii=False, indent=2) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
        print(args.output)
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
