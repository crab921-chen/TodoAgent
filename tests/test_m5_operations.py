import os
import logging
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from ops.observability import JsonFormatter, redact_text
from ops.metrics import RuntimeMetrics
from ops.runtime import ConfigurationError, load_config
from harness.hooks import RuntimeEvent
from tools.ci_checks import check_release_manifest


class M5OperationsTests(unittest.TestCase):
    def test_production_configuration_requires_secret_and_safe_dict_redacts_it(self):
        with patch.dict(
            os.environ,
            {"APP_ENV": "production", "GROQ_API_KEY": ""},
            clear=False,
        ):
            with self.assertRaises(ConfigurationError):
                load_config()

        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "GROQ_API_KEY": "g" + "sk_test_secret_value_123456",
            },
            clear=False,
        ):
            config = load_config()
            self.assertTrue(config.safe_dict()["api_key_configured"])
            self.assertNotIn("g" + "sk_test_secret", str(config.safe_dict()))

    def test_log_redactor_removes_api_key_and_authorization_values(self):
        secret = "g" + "sk_test_secret_value_123456"
        message = f"GROQ_API_KEY={secret} Authorization: Bearer {secret}"

        redacted = redact_text(message)

        self.assertNotIn(secret, redacted)
        self.assertNotIn("Bearer " + secret, redacted)
        self.assertIn("<redacted>", redacted)

        variants = (
            'Authorization: Token abcdefghijklmnop',
            'X-API-Key: "secret-value-123456"',
            '{"api_key":"secret-value-123456", "authorization":"Bearer abcdefghijklmnop"}',
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotIn("secret-value-123456", redact_text(variant))
                self.assertNotIn("abcdefghijklmnop", redact_text(variant))

    def test_json_log_formatter_preserves_request_identity_without_secrets(self):
        record = logging.LogRecord(
            "todo.test",
            logging.INFO,
            __file__,
            1,
            "provider failed: %s",
            ("g" + "sk_hidden_secret_123456",),
            None,
        )
        record.request_id = "m5-log-request"
        record.user_id = "alice"

        output = JsonFormatter().format(record)

        self.assertIn('"request_id":"m5-log-request"', output)
        self.assertNotIn("g" + "sk_hidden_secret", output)
        self.assertIn("<redacted-api-key>", output)

    def test_configuration_rejects_invalid_environment_values(self):
        invalid_values = (
            ("APP_ENV", "invalid"),
            ("GROQ_TEMPERATURE", "-1"),
            ("MODEL_TIMEOUT_SECONDS", "0"),
            ("MAX_TOOL_CALLS", "0"),
            ("LOG_LEVEL", "verbose"),
            ("GROQ_MODEL", " "),
        )
        for key, value in invalid_values:
            with self.subTest(key=key):
                with patch.dict(os.environ, {key: value}, clear=False):
                    with self.assertRaises(ConfigurationError):
                        load_config()

    def test_release_manifest_has_no_missing_operational_files_or_embedded_key(self):
        self.assertEqual(check_release_manifest(), [])

    def test_runtime_metrics_aggregate_bounded_runtime_events(self):
        metrics = RuntimeMetrics()
        metrics.emit(
            RuntimeEvent(
                kind="model",
                phase="before",
                request_id="m5-metrics",
                user_id="alice",
            )
        )
        metrics.emit(
            RuntimeEvent(
                kind="model",
                phase="after",
                request_id="m5-metrics",
                user_id="alice",
                duration_seconds=0.012,
                ok=True,
            )
        )
        metrics.emit(
            RuntimeEvent(
                kind="budget_exceeded",
                phase="occurred",
                request_id="m5-metrics",
                user_id="alice",
                error_code="TOTAL_TIME_LIMIT_REACHED",
            )
        )

        snapshot = metrics.snapshot()

        self.assertEqual(snapshot["request_total"], 1)
        self.assertEqual(snapshot["model_call_total"], 1)
        self.assertEqual(snapshot["model_call_failure_total"], 0)
        self.assertEqual(snapshot["budget_exceeded_total"], 1)
        self.assertEqual(snapshot["model_call_duration_ms"], 12)

    def test_runtime_metrics_cover_empty_failure_error_and_concurrent_snapshots(self):
        metrics = RuntimeMetrics()
        self.assertEqual(metrics.snapshot(), {"request_total": 0, "error_code_counts": {}})

        events = [
            RuntimeEvent(
                kind="model",
                phase="after",
                request_id=f"failure-{index}",
                user_id="alice",
                duration_seconds=9999,
                ok=False,
                error_code="MODEL_TIMEOUT",
            )
            for index in range(8)
        ]
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(metrics.emit, events))

        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["request_failure_total"], 8)
        self.assertEqual(snapshot["error_code_counts"]["MODEL_TIMEOUT"], 8)
        self.assertEqual(snapshot["model_call_duration_ms"], 8 * 9999000)

        metrics.emit(
            RuntimeEvent(
                kind="cancellation",
                phase="occurred",
                request_id="cancelled-request",
                user_id="alice",
                error_code="CANCELLED",
            )
        )
        self.assertEqual(metrics.snapshot()["cancellation_total"], 1)

    def test_release_manifest_rejects_missing_file_and_embedded_secret(self):
        import tools.ci_checks as ci_checks

        with patch.object(ci_checks, "ROOT", ci_checks.ROOT / "missing-root"):
            self.assertTrue(ci_checks.check_release_manifest())


if __name__ == "__main__":
    unittest.main()
