"""Environment-backed runtime configuration with safe diagnostics."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when deployment configuration is missing or invalid."""


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _path(value: str, default: Path) -> Path:
    candidate = Path(value).expanduser() if value.strip() else default
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def _float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a number") from error
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _required_text(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    if not value:
        raise ConfigurationError(f"{name} must not be blank")
    return value


@dataclass(frozen=True)
class RuntimeConfig:
    """Validated settings used by the app and operational commands."""

    app_env: str
    groq_api_key: str | None
    model_name: str
    model_base_url: str
    model_temperature: float
    model_timeout_seconds: float
    database_path: Path
    checkpoint_database_path: Path
    log_level: str
    max_tool_calls: int

    @classmethod
    def from_env(cls, *, require_secret: bool = False) -> "RuntimeConfig":
        app_env = os.getenv("APP_ENV", "development").strip().lower()
        if app_env not in {"development", "test", "staging", "production"}:
            raise ConfigurationError("APP_ENV must be development, test, staging, or production")
        # An explicitly supplied legacy GROQ variable takes precedence so a
        # deployment can intentionally clear or rotate the older secret name.
        if "GROQ_API_KEY" in os.environ:
            api_key = os.getenv("GROQ_API_KEY", "").strip() or None
        else:
            api_key = os.getenv("ZHIPU_API_KEY", "").strip() or None
        if (require_secret or app_env in {"staging", "production"}) and not api_key:
            raise ConfigurationError("ZHIPU_API_KEY is required for this environment")
        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("LOG_LEVEL is not supported")
        model_name_value = (
            os.getenv("GROQ_MODEL")
            if "GROQ_MODEL" in os.environ
            else os.getenv("MODEL_NAME", "glm-5.3")
        )
        model_name = str(model_name_value or "").strip()
        if not model_name:
            raise ConfigurationError("GROQ_MODEL/MODEL_NAME must not be blank")
        model_base_url = _required_text(
            "MODEL_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"
        )
        return cls(
            app_env=app_env,
            groq_api_key=api_key,
            model_name=model_name,
            model_base_url=model_base_url,
            model_temperature=_float(
                "GROQ_TEMPERATURE" if "GROQ_TEMPERATURE" in os.environ else "MODEL_TEMPERATURE",
                os.getenv(
                    "GROQ_TEMPERATURE" if "GROQ_TEMPERATURE" in os.environ else "MODEL_TEMPERATURE",
                    "0.0",
                ),
                minimum=0.0,
                maximum=2.0,
            ),
            model_timeout_seconds=_float(
                "MODEL_TIMEOUT_SECONDS", 60.0, minimum=0.1, maximum=600.0
            ),
            database_path=_path(
                os.getenv("TODO_DATABASE_PATH", ""),
                PROJECT_ROOT / "database" / "todo.db",
            ),
            checkpoint_database_path=_path(
                os.getenv("TODO_CHECKPOINT_DATABASE_PATH", ""),
                PROJECT_ROOT / "database" / "checkpoints.db",
            ),
            log_level=log_level,
            max_tool_calls=_int(
                "MAX_TOOL_CALLS", 8, minimum=1, maximum=100
            ),
        )

    def ensure_directories(self) -> None:
        """Create only configured parent directories, never log secret values."""

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_database_path.parent.mkdir(parents=True, exist_ok=True)

    def safe_dict(self) -> dict[str, str | int | float]:
        """Return diagnostics that intentionally omit credentials and raw paths."""

        return {
            "app_env": self.app_env,
            "model_name": self.model_name,
            "model_base_url": self.model_base_url,
            "model_temperature": self.model_temperature,
            "model_timeout_seconds": self.model_timeout_seconds,
            "log_level": self.log_level,
            "max_tool_calls": self.max_tool_calls,
            "database_configured": bool(self.database_path),
            "checkpoint_database_configured": bool(self.checkpoint_database_path),
            "api_key_configured": bool(self.groq_api_key),
        }


def load_config(*, require_secret: bool = False) -> RuntimeConfig:
    """Load and validate environment configuration."""

    return RuntimeConfig.from_env(require_secret=require_secret)
