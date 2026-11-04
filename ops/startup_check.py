"""Initialize safely, reconcile the business database, and optionally exec a command."""

from __future__ import annotations

import os
import sys

from database.maintenance import migrate_with_backup
from ops.healthcheck import check_health
from ops.observability import configure_logging
from ops.runtime import ConfigurationError, load_config


def main() -> int:
    try:
        config = load_config(require_secret=config_app_requires_secret())
        configure_logging(config.log_level)
        config.ensure_directories()
        migrate_with_backup(
            config.database_path,
            checkpoints_path=config.checkpoint_database_path,
        )
        report = check_health(config)
        if not report["ok"]:
            print("Startup health check failed.")
            return 1
    except (ConfigurationError, OSError, RuntimeError) as error:
        print(f"Startup check failed: {type(error).__name__}")
        return 1
    if len(sys.argv) > 1:
        os.execvp(sys.argv[1], sys.argv[1:])
    print("Startup check passed.")
    return 0


def config_app_requires_secret() -> bool:
    return os.getenv("APP_ENV", "development").strip().lower() in {"staging", "production"}


if __name__ == "__main__":
    raise SystemExit(main())
