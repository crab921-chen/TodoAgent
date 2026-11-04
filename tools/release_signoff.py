"""Run the documented M5 acceptance path and emit a release signoff report."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parent.parent
ACCEPTANCE_IDS = tuple(f"AT-{index:02d}" for index in range(1, 16))
TEST_MODULES = (
    "tests.test_tool_protocol",
    "tests.test_agent_loop",
    "tests.test_unified_runtime",
    "tests.test_m1_remaining",
    "tests.test_m2_data",
    "tests.test_m3_data",
    "tests.test_m4_data",
    "tests.test_m5_delivery",
    "tests.test_m5_operations",
)


def _run(label: str, command: list[str], env: dict[str, str] | None = None) -> dict[str, object]:
    started = time.monotonic()
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return {
        "name": label,
        "ok": result.returncode == 0,
        "return_code": result.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "output_tail": result.stdout[-1000:],
    }


def run_signoff() -> dict[str, object]:
    unique = uuid4().hex
    database_path = ROOT / "database" / f"release-signoff-{unique}.db"
    checkpoint_path = ROOT / "database" / f"release-signoff-checkpoints-{unique}.db"
    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "test",
            "GROQ_API_KEY": "",
            "TODO_DATABASE_PATH": str(database_path),
            "TODO_CHECKPOINT_DATABASE_PATH": str(checkpoint_path),
        }
    )
    checks = [
        _run(
            "ci_release_checks",
            [sys.executable, "tools/ci_checks.py", "--release"],
            env,
        ),
        _run(
            "compile_check",
            [
                sys.executable,
                "-m",
                "compileall",
                "-q",
                "app.py",
                "cli.py",
                "database",
                "graph",
                "harness",
                "legacy",
                "ops",
                "prompts",
                "tests",
                "tools",
            ],
            env,
        ),
        _run(
            "acceptance_tests",
            [sys.executable, "-m", "unittest", *TEST_MODULES, "-q"],
            env,
        ),
        _run(
            "clean_startup",
            [sys.executable, "-m", "ops.startup_check"],
            env,
        ),
    ]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "acceptance_ids": ACCEPTANCE_IDS,
        "p0_gate": all(bool(check["ok"]) for check in checks),
        "checks": checks,
        "known_limitations": [
            "Single-host Streamlit deployment with SQLite.",
            "Username entry is not production authentication.",
            "External model availability and rate limits remain dependencies.",
            "External alert routing and secret rotation remain platform responsibilities.",
        ],
    }
    for path in (database_path, checkpoint_path):
        path.unlink(missing_ok=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Todo Agent release signoff")
    parser.add_argument("--output", type=Path, help="Write the JSON evidence report")
    args = parser.parse_args()
    report = run_signoff()
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = args.output if args.output.is_absolute() else ROOT / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0 if report["p0_gate"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
