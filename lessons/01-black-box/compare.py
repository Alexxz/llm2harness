"""Compare saved coursekit response texts without contacting a model."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    args = parser.parse_args()
    try:
        left = json.loads(args.left.read_text(encoding="utf-8"))["text"]
        right = json.loads(args.right.read_text(encoding="utf-8"))["text"]
        if not isinstance(left, str) or not isinstance(right, str):
            raise ValueError("text must be a string")
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))
    print(json.dumps({"same_text": left == right, "left_characters": len(left), "right_characters": len(right)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
