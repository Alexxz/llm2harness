#!/usr/bin/env python3
"""Protocol-only demonstration. Replace the explicit failure with your agent call."""
import json
import sys

def main():
    request = json.load(sys.stdin)
    # Feed request['instructions'], request['input'] and request['schema'] to
    # your agent's text-only API with timeout=request['timeout'].
    # Disable tools and inherited context. Validate the JSON before printing it.
    # print(json.dumps(answer, ensure_ascii=False))
    print('Protocol example has no model connected', file=sys.stderr)
    return 1

if __name__ == '__main__':
    raise SystemExit(main())
