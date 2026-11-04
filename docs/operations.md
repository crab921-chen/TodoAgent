# Todo Agent Operations

This runbook covers the single-host deployment described by the project
documents. It deliberately keeps API keys outside source code, images, logs,
SQLite files, and backup manifests.

The repository layout is documented in `docs/code_layout.md`. The `legacy/`
tree contains the original LangGraph implementation retained for compatibility
and comparison; the public Streamlit/CLI experience uses the maintained M1-M5
runtime under `harness/`, `database/`, and `ops/`.

## Clean startup

1. Copy `.env.example` to `.env` and set `GROQ_API_KEY` outside version control.
2. Review the model, timeout, tool budget, database paths, and log level.
3. Run `python -m ops.startup_check`.
4. Start the UI with `streamlit run app.py`, or use `docker compose -f deploy/docker-compose.yml up -d`.
5. Run `python -m ops.healthcheck --json` after startup.

Production and staging require `GROQ_API_KEY`. Development and test startup can
validate the database without contacting the model provider.

## Upgrade and rollback

`ops.startup_check` uses `migrate_with_backup`: it verifies the existing SQLite
database, creates a backup before migrations, applies versioned migrations, and
reconciles integrity, foreign keys, schema versions, and row counts. A failed
migration restores the backup and exits non-zero.

For a separate rehearsal, use the M5 tests or call `backup_bundle` and
`restore_bundle` from `database.maintenance`. Restore into a clean directory,
run the migration, then verify tasks, reminders, transactions, and the health
report before switching the application to the restored files.

## Backup policy

Back up `database/todo.db` at the configured operational interval. Back up
`database/checkpoints.db` separately only when conversation history is in the
retention scope. Keep manifests and backups outside the application image,
restrict filesystem permissions, and periodically perform a clean-environment
restore rehearsal.

## Logging and monitoring

Runtime logs are JSON-shaped and correlated by `request_id` when emitted through
the Harness hooks. Tool audit rows store a digest and bounded type summaries,
not raw arguments. API keys, authorization tokens, prompts, complete financial
notes, and full database rows must not be added to ordinary logs.

Monitor startup failures, model failures and timeouts, tool error codes,
SQLite lock wait failures, reminder processing/failed/expired counts, backup
success and age, and the latest migration version. Use the health command for a
safe database and configuration report; it never prints the key.

## Troubleshooting

* `GROQ_API_KEY is required`: configure the secret in the process environment
  or an untracked `.env`; do not put it in YAML, source, or a log message.
* Database integrity failure: stop writes, preserve the failed files, inspect
  the health report, and restore the latest verified bundle before retrying.
* Migration failure: keep the non-zero result and backup manifest; the startup
  path restores the pre-migration database automatically.
* SQLite lock errors: check for a second writer, long-running transactions,
  disk space, and recent process restarts. Retry only the operation marked
  retryable and rely on idempotency keys for business writes.
* Reminder delivery failures: inspect status, attempt count, next retry time,
  and the associated request/audit identifier. Do not manually insert a second
  occurrence.
* Model timeout or provider failure: check provider availability and configured
  timeout/model settings. A failed response must not be treated as a failed
  committed business transaction.

## Known limitations

This release is a single-host Streamlit application using SQLite. The username
screen is not production authentication, SQLite is not a distributed write
store, and the reminder scheduler is not a multi-node queue. The model provider
is external and may be unavailable or rate-limited. Checkpoint history may be
omitted from backups unless explicitly enabled by policy. Log retention,
external alert routing, and secret rotation remain host/platform concerns.
