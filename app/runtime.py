from __future__ import annotations

import logging
import hashlib
from pathlib import Path

from app.core import AssistantCore
from app.pairing import OwnerGate, PairingManager
from app.pymax_transport import PyMaxOptions, PyMaxTransport
from app.queue import PerChatQueue
from app.storage import Storage
from app.storage import DailyLimitExceeded
from app.transport import IncomingAttachment
from app.providers.openai_compatible import (
    OpenAIAuthenticationError,
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
    OpenAIInputTooLong,
    OpenAIPermissionError,
    OpenAIProviderError,
    OpenAIQuotaError,
    OpenAITransientError,
)


async def echo_responder(
    text: str,
    history: list[tuple[str, str]],
    attachments: tuple[IncomingAttachment, ...] = (),
) -> str:
    return f"echo: {text}"


async def run_gate0(
    storage: Storage,
    transport: PyMaxTransport,
    pairing: PairingManager,
    claim_command_path: Path,
    max_message_chars: int,
) -> None:
    logger = logging.getLogger("max_ai_assistant.runtime")
    core = AssistantCore(
        storage=storage,
        transport=transport,
        responder=echo_responder,
        pairing=pairing,
        owner_gate=OwnerGate(storage),
        queue=PerChatQueue(),
        max_message_chars=max_message_chars,
    )
    try:
        async for message in transport.messages():
            try:
                result = await core.handle(message)
                if result == "claimed":
                    claim_command_path.unlink(missing_ok=True)
                    logger.info("Owner claim completed")
                    return
            except Exception as exc:
                logger.error(
                    "Message handling failed message_id=%s error=%s",
                    message.message_id,
                    type(exc).__name__,
                )
    finally:
        await transport.close()


class LLMResponder:
    def __init__(self, provider: OpenAICompatibleProvider, storage: Storage) -> None:
        self.provider = provider
        self.storage = storage
        self.provider_name = provider.config.provider_name
        self.provider_label = {
            "openrouter": "OpenRouter",
            "openai": "OpenAI",
        }.get(self.provider_name, self.provider_name)
        self.logger = logging.getLogger("max_ai_assistant.ai_responder")

    async def __call__(
        self,
        text: str,
        history: list[tuple[str, str]],
        attachments: tuple[IncomingAttachment, ...] = (),
    ) -> str:
        command = text.strip().lower()
        if not attachments and command == "/new":
            return "Новый разговор начат. Предыдущий контекст больше не используется."
        if not attachments and command == "/help":
            return (
                "Доступны текстовый диалог, документы, изображения и аудио. Команды: "
                "/new — новый разговор, /status — безопасный статус, /help — эта справка."
            )
        if not attachments and command == "/status":
            usage = self.storage.get_daily_usage(self.provider._today())
            return (
                f"MAX: подключён. AI: {self.provider_label} Responses API. Модель: "
                f"{self.provider.config.model}. Сегодня: {usage.requests}/"
                f"{self.provider.config.daily_request_limit} запросов, "
                f"{usage.input_tokens + usage.output_tokens}/"
                f"{self.provider.config.daily_token_limit} токенов."
            )
        try:
            unsupported = [item for item in attachments if item.kind == "unsupported"]
            if unsupported:
                names = ", ".join(item.filename for item in unsupported[:3])
                return f"Не могу обработать вложение: {names}. Поддерживаются документы, таблицы, презентации, изображения и аудио."
            return await self.provider.complete(history, attachments=attachments)
        except DailyLimitExceeded:
            return "Дневной лимит AI исчерпан. Попробуйте после обновления лимита."
        except OpenAIInputTooLong:
            return "Сообщение слишком длинное. Сократите его и попробуйте снова."
        except OpenAIAuthenticationError:
            self.logger.error("LLM authentication failed provider=%s", self.provider_name)
            return f"{self.provider_label} отклонил API-ключ. Проверьте локальный secret-файл."
        except OpenAIPermissionError as exc:
            code = exc.details.get("code")
            if code is not None and str(code) == str(exc.status):
                code = None
            reason = code or exc.details.get("type") or "forbidden"
            self.logger.error(
                "LLM permission denied provider=%s status=%s reason=%s",
                self.provider_name,
                exc.status,
                reason,
            )
            return (
                f"{self.provider_label} запретил запрос: HTTP {exc.status or 403}, "
                f"причина {reason}."
            )
        except OpenAIQuotaError:
            self.logger.warning("LLM quota is unavailable provider=%s", self.provider_name)
            return f"У {self.provider_label} закончилась доступная квота."
        except OpenAITransientError:
            self.logger.warning("LLM request failed after retries provider=%s", self.provider_name)
            return f"{self.provider_label} временно недоступен. Попробуйте немного позже."
        except OpenAIProviderError as exc:
            self.logger.error("LLM provider error type=%s", type(exc).__name__)
            return f"Не удалось получить корректный ответ {self.provider_label}."


async def run_ai(
    storage: Storage,
    transport: PyMaxTransport,
    responder: LLMResponder,
    pairing: PairingManager,
    claim_command_path: Path,
    max_message_chars: int,
    history_limit: int,
) -> None:
    core = AssistantCore(
        storage=storage,
        transport=transport,
        responder=responder,
        pairing=pairing,
        owner_gate=OwnerGate(storage),
        queue=PerChatQueue(),
        max_message_chars=max_message_chars,
        history_limit=history_limit,
    )
    try:
        async for message in transport.messages():
            try:
                await core.handle(message)
            except Exception as exc:
                logging.getLogger("max_ai_assistant.runtime").error(
                    "AI message handling failed message_id=%s error=%s",
                    message.message_id,
                    type(exc).__name__,
                )
    finally:
        await responder.provider.close()
        await transport.close()


def make_pymax_transport(settings: object) -> PyMaxTransport:
    return PyMaxTransport(
        PyMaxOptions(
            session_path=settings.max_session_path,
            qr_path=settings.qr_path,
            status_path=settings.qr_status_path,
            two_factor_secret_path=settings.two_factor_secret_path,
            log_level=settings.log_level,
            reconnect_delay=settings.max_reconnect_delay,
            locale=settings.max_locale,
            timezone=settings.max_timezone,
            header_user_agent=settings.max_web_header_user_agent,
            max_attachment_count=settings.max_attachment_count,
            max_attachment_bytes=settings.max_attachment_bytes,
        )
    )


def make_llm_responder(
    settings: object, storage: Storage, *, source: str = "max"
) -> LLMResponder:
    owner = storage.get_owner()
    if owner is None:
        raise RuntimeError("Owner must be paired before enabling AI")
    safety_hash = hashlib.sha256(
        f"max-owner:{owner.max_user_id}".encode("utf-8")
    ).hexdigest()[:32]
    provider = OpenAICompatibleProvider(
        OpenAICompatibleConfig(
            base_url=settings.llm_base_url,
            api_key_file=settings.llm_api_key_file,
            model=settings.llm_model,
            provider_name=settings.llm_provider,
            timeout_seconds=settings.llm_timeout,
            max_retries=settings.llm_max_retries,
            max_output_tokens=settings.llm_max_output_tokens,
            daily_request_limit=settings.llm_daily_limit,
            daily_token_limit=settings.llm_daily_token_limit,
            max_input_chars=settings.llm_max_input_chars,
            timezone_offset_minutes=settings.llm_timezone_offset_minutes,
            safety_identifier=f"max_{safety_hash}",
            instructions=settings.llm_instructions,
            source=source,
            transcription_model=settings.llm_transcription_model,
            max_attachment_bytes=settings.max_attachment_bytes,
        ),
        storage,
    )
    return LLMResponder(provider, storage)


# Backward-compatible factory name for existing local tooling.
make_openai_responder = make_llm_responder
