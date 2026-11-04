"""Read-only health check for deployment probes and incident diagnosis."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from database.maintenance import reconcile_database
from ops.runtime import ConfigurationError, RuntimeConfig, load_config


def _check_sqlite(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        return {"status": "missing" if required else "not_initialized", "ok": not required}
    try:
        connection = sqlite3.connect(str(path), timeout=5)
        try:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            connection.close()
        return {"status": "ok" if integrity == "ok" else "failed", "ok": integrity == "ok"}
    except sqlite3.Error as error:
        return {"status": "failed", "ok": False, "error": type(error).__name__}


def check_health(config: RuntimeConfig | None = None) -> dict[str, Any]:
    """Return safe health details without exposing paths, prompts, or secrets."""

    config = config or load_config()
    checks: dict[str, Any] = {"configuration": {"ok": True, **config.safe_dict()}}
    try:
        database = reconcile_database(config.database_path)
        checks["database"] = {
            "ok": database["ok"],
            "integrity_check": database["integrity_check"],
            "foreign_key_errors": len(database["foreign_key_errors"]),
            "schema_versions": database["schema_versions"],
        }
    except Exception as error:
        checks["database"] = {"ok": False, "error": type(error).__name__}
    checks["checkpoint_database"] = _check_sqlite(
        config.checkpoint_database_path,
        required=False,
    )
    return {"ok": all(item.get("ok", False) for item in checks.values()), "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Todo Agent runtime health")
    parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    args = parser.parse_args()
    try:
        report = check_health()
    except ConfigurationError as error:
        report = {"ok": False, "checks": {"configuration": {"ok": False, "error": str(error)}}}
    output = json.dumps(report, ensure_ascii=False, separators=(",", ":") if args.json else None)
    print(output)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
