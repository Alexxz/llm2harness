"""Trusted syntax/schema-only harness check using the documented interpreter."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
from coursekit.classroom import Session


def main():
    context = json.loads((HERE / "context.json").read_text())
    session = Session.open(context["session_id"], root=Path(context["temporary_root"]))
    run = session.snapshot()["last_run"]
    response = HERE / (run["id"] + "-raw-response.txt")
    command = ["/usr/bin/python3", "-m", "coursekit", "check-json", "--schema",
               str(Path(context["temporary_root"]) / "lessons/03-structured-prompts/schema.json"),
               "--input", str(response)]
    checked = subprocess.run(command, cwd=context["source_root"], capture_output=True, text=True, timeout=10)
    record = {"source": "trusted_test_harness_on_synthetic_http_response", "run_id": run["id"], "command": command,
              "exit_code": checked.returncode, "stdout": checked.stdout, "stderr": checked.stderr,
              "response_sha256": hashlib.sha256(response.read_bytes()).hexdigest(),
              "scope": "syntax_and_schema_only; no semantic result inferred",
              "setup_correction": "Documented /usr/bin/python3 has jsonschema; original pyenv attempt retained unchanged. No HTTP request was repeated.",
              "timestamp": datetime.now(timezone.utc).isoformat()}
    with (HERE / "validation.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    session.write_artifact("validator.json", json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(record, ensure_ascii=False))


if __name__ == "__main__":
    main()
