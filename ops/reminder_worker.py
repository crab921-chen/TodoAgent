"""Single-host reminder worker for the durable SQLite reminder queue."""

from __future__ import annotations

import argparse
import logging
import time
from typing import Any

from database.m3 import ReminderScheduler
from database.schema import initialize_database
from ops.runtime import ConfigurationError, load_config


LOGGER = logging.getLogger(__name__)


def console_sender(reminder: dict[str, Any]) -> None:
    """Deliver one reminder through the host process output channel."""

    print(f"Reminder #{reminder['id']}: {reminder['content']}", flush=True)


class ReminderWorker:
    """Run recover, expire, claim, delivery, and retry as one worker cycle."""

    def __init__(
        self,
        sender=console_sender,
        *,
        max_attempts: int = 3,
        retry_delay_seconds: int = 60,
        stale_after_seconds: int = 300,
        expire_after_seconds: int = 86400,
    ) -> None:
        self.scheduler = ReminderScheduler(
            sender,
            max_attempts=max_attempts,
            retry_delay_seconds=retry_delay_seconds,
            stale_after_seconds=stale_after_seconds,
            expire_after_seconds=expire_after_seconds,
        )

    def run_once(self, *, limit: int = 100) -> list[dict[str, Any]]:
        results = self.scheduler.run_once(limit=limit)
        for result in results:
            if result["status"] == "failed":
                LOGGER.error(
                    "Reminder delivery failed",
                    extra={"reminder_id": result["reminder"]["id"]},
                )
        return results

    def run_forever(self, *, interval_seconds: int = 30, limit: int = 100) -> None:
        if interval_seconds < 1:
            raise ValueError("interval_seconds must be positive")
        while True:
            self.run_once(limit=limit)
            time.sleep(interval_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Todo Agent reminder worker")
    parser.add_argument("--once", action="store_true", help="Run one delivery cycle")
    parser.add_argument("--interval-seconds", type=int, default=30)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retry-delay-seconds", type=int, default=60)
    parser.add_argument("--stale-after-seconds", type=int, default=300)
    parser.add_argument("--expire-after-seconds", type=int, default=86400)
    args = parser.parse_args()
    try:
        config = load_config()
        config.ensure_directories()
        initialize_database(config.database_path)
        worker = ReminderWorker(
            max_attempts=args.max_attempts,
            retry_delay_seconds=args.retry_delay_seconds,
            stale_after_seconds=args.stale_after_seconds,
            expire_after_seconds=args.expire_after_seconds,
        )
        if args.once:
            worker.run_once(limit=args.limit)
        else:
            worker.run_forever(interval_seconds=args.interval_seconds, limit=args.limit)
    except (ConfigurationError, OSError, RuntimeError, ValueError) as error:
        print(f"Reminder worker failed: {type(error).__name__}: {error}")
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ReminderWorker", "console_sender", "main"]
