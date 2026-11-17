"""A manually dispatched, read-only lesson tool with a single allowed logical path."""

import argparse
import json
from pathlib import Path


def extract(value):
    if isinstance(value, dict) and value.get("fixture_kind") == "offline_fixture":
        return value["model_response"]
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return json.loads(value["text"])
    return value


def dispatch(proposal, source):
    # The model supplies a logical name, never the host filesystem path.
    required = {"action": "read_file", "arguments": {"path": "stock.json"}}
    if proposal != required:
        return {"ok": False, "error": "policy_denied", "message": "Only read_file(path=stock.json) is allowed"}
    # The source is chosen by the human operator, separately from the proposal.
    if source.is_symlink():
        return {"ok": False, "error": "source_denied", "message": "Symlink sources are not allowed"}
    try:
        with source.open("rb") as stream:
            data = stream.read(8193)
        if len(data) > 8192:
            return {"ok": False, "error": "result_too_large"}
        content = json.loads(data)
    except (OSError, ValueError) as error:
        return {"ok": False, "error": "read_failed", "message": str(error)}
    return {"ok": True, "tool": "read_file", "arguments": proposal["arguments"], "result": content}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=Path(__file__).parent / "fixtures/stock.json", help="Synthetic source chosen by the human, never by model arguments")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        proposal = extract(json.loads(args.proposal.read_text(encoding="utf-8")))
        result = dispatch(proposal, args.source)
    except (OSError, ValueError, KeyError, TypeError) as error:
        result = {"ok": False, "error": "invalid_action", "message": str(error)}
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
