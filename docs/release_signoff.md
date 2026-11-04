# M5 Release Signoff

The release gate is the repeatable command below. It runs the CI release
checks, compile check, all M1-M5 automated tests, and a clean test-environment
startup with isolated database paths.

```text
python tools/release_signoff.py --output release-signoff.json
```

The JSON report is release evidence and should be retained as a CI artifact,
not committed to the repository. The P0 gate is passable only when every check
is successful.

## Acceptance mapping

| ID | Acceptance path | Evidence |
| --- | --- | --- |
| AT-01 | Multi-turn tool execution | `tests.test_agent_loop` |
| AT-02 | Tool/model budget termination | `tests.test_agent_loop`, `tests.test_m1_remaining` |
| AT-03 | Unknown tool and invalid arguments | `tests.test_tool_protocol`, `tests.test_agent_loop` |
| AT-04 | High-risk confirmation | `tests.test_m1_remaining`, `tests.test_m2_data`, `tests.test_m4_data` |
| AT-05 | Cross-user isolation | `tests.test_m2_data`, `tests.test_m3_data`, `tests.test_m4_data`, `tests.test_m5_delivery` |
| AT-06 | Task state machine | `tests.test_m2_data` |
| AT-07 | Idempotent occurrence/retry behavior | `tests.test_m2_data`, `tests.test_m3_data`, `tests.test_m5_delivery` |
| AT-08 | Timezone and DST boundaries | `tests.test_m2_data`, `tests.test_m4_data` |
| AT-09 | Reminder recovery and duplicate scan protection | `tests.test_m3_data`, `tests.test_m5_delivery` |
| AT-10 | Recurrence and goal progress | `tests.test_m3_data` |
| AT-11 | Statistics consistency | `tests.test_m4_data` |
| AT-12 | Integer minor-unit finance precision | `tests.test_m4_data` |
| AT-13 | Explicit task/transaction links | `tests.test_m4_data` |
| AT-14 | Backup, restore, migration rollback | `tests.test_m5_delivery` |
| AT-15 | Log redaction and request-correlated operations | `tests.test_m5_operations` |

## Release conditions

* All P0 acceptance IDs are covered by automated evidence and pass.
* CI format/static checks, tests, compile/startup checks, and container build pass.
* `.env.example` contains no real credential; production secrets are injected
  through the environment or a secret manager.
* A clean-environment startup and a verified backup restore rehearsal have been
  completed for the release candidate.
* Known limitations remain visible to the owner: single-host SQLite,
  username-based access, external model availability, and platform-owned alert
  routing/secret rotation.

## Signoff record

```text
Release: ____________________
Commit: _____________________
Evidence artifact: __________
Test owner: _________________
Operations owner: ___________
Decision: PASS / HOLD
Notes: ______________________
```
