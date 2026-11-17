"""Prepare identical evaluation requests with and without in-context examples."""

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MARKER = "Текущая заявка:"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        cases = json.loads((ROOT / "fixtures/evaluation-cases.json").read_text(encoding="utf-8"))
        for mode in ["baseline", "variation"]:
            template = (ROOT / "prompts" / f"extended-{mode}.txt").read_text(encoding="utf-8")
            if template.count(MARKER) != 1:
                raise ValueError(f"{mode} must contain one current-request marker")
            prefix = template.split(MARKER, 1)[0]
            destination = args.output / mode
            destination.mkdir(parents=True, exist_ok=True)
            for case in cases:
                prompt = prefix + MARKER + " " + case["request"] + "\nОтвет:\n"
                path = destination / f"{case['id']}.txt"
                path.write_text(prompt, encoding="utf-8")
        print(json.dumps({"prepared_inputs": len(cases) * 2, "output": str(args.output)}, ensure_ascii=False))
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
