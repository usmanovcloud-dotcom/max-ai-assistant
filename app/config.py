from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    database_path: Path
    max_session_path: Path
    qr_path: Path
    qr_status_path: Path
    two_factor_secret_path: Path
    llm_api_key_file: Path
    claim_command_path: Path
    log_level: str = "INFO"
    claim_ttl_seconds: int = 900
    max_message_chars: int = 3500
    pymax_telemetry_enabled: bool = False
    max_reconnect_delay: float = 2.0
    max_locale: str = "ru"
    max_timezone: str = "Asia/Yekaterinburg"
    max_web_header_user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    )
    llm_provider: str = "openai"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-5.6-luna"
    llm_timeout: float = 60.0
    llm_daily_limit: int = 50
    llm_daily_token_limit: int = 100_000
    llm_max_output_tokens: int = 1200
    llm_max_retries: int = 2
    llm_history_messages: int = 30
    llm_max_input_chars: int = 24_000
    llm_timezone_offset_minutes: int = 300
    llm_instructions: str = (
        "Ты личный AI-ассистент владельца. Отвечай по-русски, если пользователь "
        "не попросил иначе. Будь практичным и не утверждай, что выполнил внешнее "
        "действие, если у тебя нет соответствующего инструмента."
    )
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    web_autostart_ai: bool = True
    openai_monthly_budget_usd: float = 10.0
    container_mode: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("APP_DATA_DIR", "data")).expanduser().resolve()
        llm_provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
        provider_defaults = {
            "openrouter": (
                "https://openrouter.ai/api/v1",
                "openrouter/free",
                "secrets/openrouter-api-key.txt",
            ),
            "openai": (
                "https://api.openai.com/v1",
                "gpt-5.6-luna",
                "secrets/openai-api-key.txt",
            ),
        }
        default_base_url, default_model, default_key_file = provider_defaults.get(
            llm_provider, ("", "", "secrets/llm-api-key.txt")
        )
        settings = cls(
            data_dir=data_dir,
            database_path=Path(
                os.getenv("APP_DATABASE_PATH", str(data_dir / "assistant.sqlite3"))
            ).expanduser().resolve(),
            max_session_path=Path(
                os.getenv("MAX_SESSION_PATH", str(data_dir / "max-session.sqlite3"))
            ).expanduser().resolve(),
            qr_path=Path(os.getenv("MAX_QR_PATH", str(data_dir / "login.svg"))).expanduser().resolve(),
            qr_status_path=Path(
                os.getenv("MAX_QR_STATUS_PATH", str(data_dir / "qr-status.json"))
            ).expanduser().resolve(),
            two_factor_secret_path=Path(
                os.getenv("MAX_2FA_FILE", "secrets/max-2fa.txt")
            ).expanduser().resolve(),
            llm_api_key_file=Path(
                os.getenv("LLM_API_KEY_FILE", default_key_file)
            ).expanduser().resolve(),
            claim_command_path=Path(
                os.getenv("CLAIM_COMMAND_PATH", str(data_dir / "claim-command.txt"))
            ).expanduser().resolve(),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            claim_ttl_seconds=int(os.getenv("CLAIM_TTL_SECONDS", "900")),
            max_message_chars=int(os.getenv("MAX_MESSAGE_CHARS", "3500")),
            pymax_telemetry_enabled=_env_bool("PYMAX_TELEMETRY_ENABLED", False),
            max_reconnect_delay=float(os.getenv("MAX_RECONNECT_DELAY", "2.0")),
            max_locale=os.getenv("MAX_LOCALE", "ru"),
            max_timezone=os.getenv("MAX_TIMEZONE", "Asia/Yekaterinburg"),
            max_web_header_user_agent=os.getenv(
                "MAX_WEB_HEADER_USER_AGENT",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            ),
            llm_provider=llm_provider,
            llm_base_url=os.getenv("LLM_BASE_URL", default_base_url),
            llm_model=os.getenv("LLM_MODEL", default_model),
            llm_timeout=float(os.getenv("LLM_TIMEOUT", "60")),
            llm_daily_limit=int(os.getenv("LLM_DAILY_LIMIT", "50")),
            llm_daily_token_limit=int(os.getenv("LLM_DAILY_TOKEN_LIMIT", "100000")),
            llm_max_output_tokens=int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "1200")),
            llm_max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
            llm_history_messages=int(os.getenv("LLM_HISTORY_MESSAGES", "30")),
            llm_max_input_chars=int(os.getenv("LLM_MAX_INPUT_CHARS", "24000")),
            llm_timezone_offset_minutes=int(
                os.getenv("LLM_TIMEZONE_OFFSET_MINUTES", "300")
            ),
            llm_instructions=os.getenv(
                "LLM_INSTRUCTIONS",
                "Ты личный AI-ассистент владельца. Отвечай по-русски, если пользователь "
                "не попросил иначе. Будь практичным и не утверждай, что выполнил внешнее "
                "действие, если у тебя нет соответствующего инструмента.",
            ),
            web_host=os.getenv("WEB_HOST", "127.0.0.1"),
            web_port=int(os.getenv("WEB_PORT", "8765")),
            web_autostart_ai=_env_bool("WEB_AUTOSTART_AI", True),
            openai_monthly_budget_usd=float(
                os.getenv("OPENAI_MONTHLY_BUDGET_USD", "10")
            ),
            container_mode=_env_bool("CONTAINER_MODE", False),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.claim_ttl_seconds < 60:
            raise ValueError("CLAIM_TTL_SECONDS must be at least 60")
        if self.max_message_chars < 100:
            raise ValueError("MAX_MESSAGE_CHARS must be at least 100")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL is invalid")
        if self.pymax_telemetry_enabled:
            raise ValueError("PyMax telemetry must remain disabled")
        if self.max_reconnect_delay < 0.5:
            raise ValueError("MAX_RECONNECT_DELAY must be at least 0.5")
        if not self.max_locale.strip() or not self.max_timezone.strip():
            raise ValueError("MAX locale and timezone must not be empty")
        if not self.max_web_header_user_agent.strip():
            raise ValueError("MAX_WEB_HEADER_USER_AGENT must not be empty")
        if self.llm_provider not in {"openai", "openrouter"}:
            raise ValueError("LLM_PROVIDER must be openai or openrouter")
        if not self.llm_base_url.startswith("https://"):
            raise ValueError("LLM_BASE_URL must use HTTPS")
        if not self.llm_model.strip():
            raise ValueError("LLM_MODEL must not be empty")
        if self.llm_timeout < 5:
            raise ValueError("LLM_TIMEOUT must be at least 5 seconds")
        if self.llm_daily_limit < 1 or self.llm_daily_token_limit < 1:
            raise ValueError("LLM daily limits must be positive")
        if self.llm_max_output_tokens < 16 or self.llm_history_messages < 1:
            raise ValueError("LLM output and history limits are invalid")
        if self.llm_max_retries not in range(0, 6):
            raise ValueError("LLM_MAX_RETRIES must be between 0 and 5")
        if self.llm_max_input_chars < 100:
            raise ValueError("LLM_MAX_INPUT_CHARS must be at least 100")
        if not -720 <= self.llm_timezone_offset_minutes <= 840:
            raise ValueError("LLM_TIMEZONE_OFFSET_MINUTES is invalid")
        if not self.llm_instructions.strip():
            raise ValueError("LLM_INSTRUCTIONS must not be empty")
        allowed_web_hosts = {"127.0.0.1", "localhost", "::1"}
        if self.container_mode:
            allowed_web_hosts.add("0.0.0.0")
        if self.web_host not in allowed_web_hosts:
            raise ValueError("WEB_HOST must remain loopback-only outside a container")
        if not 1024 <= self.web_port <= 65535:
            raise ValueError("WEB_PORT must be between 1024 and 65535")
        if self.openai_monthly_budget_usd <= 0:
            raise ValueError("OPENAI_MONTHLY_BUDGET_USD must be positive")

    def ensure_runtime_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_session_path.parent.mkdir(parents=True, exist_ok=True)
        self.qr_path.parent.mkdir(parents=True, exist_ok=True)
        self.qr_status_path.parent.mkdir(parents=True, exist_ok=True)
        self.two_factor_secret_path.parent.mkdir(parents=True, exist_ok=True)
        self.llm_api_key_file.parent.mkdir(parents=True, exist_ok=True)
        self.claim_command_path.parent.mkdir(parents=True, exist_ok=True)
