"""Trusted functional oracle. Generated Bash runs only through DockerSandbox."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

CASES = [
    ("syntax", "bash -n greet.sh"),
    ("ordinary", r"""bash greet.sh 'Оля' >/tmp/out 2>/tmp/err
rc=$?
printf 'Привет, Оля!\n' >/tmp/expected
[[ "$rc" -eq 0 ]] && cmp -s /tmp/out /tmp/expected && [[ ! -s /tmp/err ]]"""),
    ("spaces", r"""bash greet.sh 'Мир и Оля' >/tmp/out 2>/tmp/err
rc=$?
printf 'Привет, Мир и Оля!\n' >/tmp/expected
[[ "$rc" -eq 0 ]] && cmp -s /tmp/out /tmp/expected && [[ ! -s /tmp/err ]]"""),
    ("empty_argument", r"""bash greet.sh '' >/tmp/out 2>/tmp/err
rc=$?
printf 'Привет, !\n' >/tmp/expected
[[ "$rc" -eq 0 ]] && cmp -s /tmp/out /tmp/expected && [[ ! -s /tmp/err ]]"""),
    ("literal_shell_characters", r"""name='$(printf SHOULD_STAY_LITERAL); *'
bash greet.sh "$name" >/tmp/out 2>/tmp/err
rc=$?
printf 'Привет, %s!\n' "$name" >/tmp/expected
[[ "$rc" -eq 0 ]] && cmp -s /tmp/out /tmp/expected && [[ ! -s /tmp/err ]]"""),
    ("missing_argument", r"""bash greet.sh >/tmp/out 2>/tmp/err
rc=$?
printf 'usage: greet.sh NAME\n' >/tmp/expected
[[ "$rc" -eq 2 ]] && [[ ! -s /tmp/out ]] && cmp -s /tmp/err /tmp/expected"""),
    ("extra_argument", r"""bash greet.sh 'Оля' 'Мир' >/tmp/out 2>/tmp/err
rc=$?
printf 'usage: greet.sh NAME\n' >/tmp/expected
[[ "$rc" -eq 2 ]] && [[ ! -s /tmp/out ]] && cmp -s /tmp/err /tmp/expected"""),
]

DIAGNOSTICS = r"""
check_rc=$?
printf 'program_exit=%s\n' "${rc:-not_applicable}"
if [[ -f /tmp/out ]]; then
  printf '%s\n' '--- program stdout ---'
  cat /tmp/out
fi
if [[ -f /tmp/err ]]; then
  printf '%s\n' '--- program stderr ---'
  cat /tmp/err
fi
printf '\ncheck_exit=%s\n' "$check_rc"
exit "$check_rc"
"""


def permitted_workspace(value: str) -> Path:
    candidate = Path(value).absolute()
    boundary = ROOT / "results"
    if boundary.is_symlink():
        raise ValueError("results must be a real course directory")
    resolved = candidate.resolve()
    if resolved == boundary or not resolved.is_relative_to(boundary):
        raise ValueError("workspace must be a dedicated subdirectory of course results/")
    if not resolved.is_dir():
        raise ValueError("workspace directory does not exist")
    if candidate != resolved:
        raise ValueError("workspace must not use symlinks or parent traversal")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace")
    parser.add_argument("--describe", action="store_true", help="Print checks; no container or model call")
    args = parser.parse_args()
    if args.describe:
        print(json.dumps({"mode": "oracle_description", "cases": [
            {"name": name, "container_script": code} for name, code in CASES
        ]}, ensure_ascii=False, indent=2))
        return 0
    if not args.workspace:
        parser.error("--workspace is required unless --describe is used")
    try:
        workspace = permitted_workspace(args.workspace)
        target = workspace / "greet.sh"
        if target.is_symlink() or not target.is_file():
            print(json.dumps({"mode": "container_oracle", "pass": False,
                              "status": "missing_artifact",
                              "error": "need a regular greet.sh in workspace"},
                             ensure_ascii=False, indent=2))
            return 1
        from coursekit.sandbox import DockerSandbox

        sandbox = DockerSandbox()
        results = []
        for name, code in CASES:
            result = sandbox.run(workspace, code + "\n" + DIAGNOSTICS)
            if result.get("timed_out") or result.get("exit_code") == 125:
                print(json.dumps({"mode": "container_oracle", "pass": False,
                                  "status": "execution_unavailable", "case": name,
                                  "result": result}, ensure_ascii=False, indent=2))
                return 3
            results.append({"name": name, "pass": result.get("exit_code") == 0,
                            "result": result})
        passed = all(item["pass"] for item in results)
        print(json.dumps({"mode": "container_oracle", "pass": passed,
                          "status": "checked", "cases": results},
                         ensure_ascii=False, indent=2))
        return 0 if passed else 1
    except Exception as exc:
        print(json.dumps({"mode": "container_oracle", "pass": False,
                          "status": "execution_unavailable",
                          "error": str(exc)}, ensure_ascii=False, indent=2))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

