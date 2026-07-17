from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.storage import DailyLimitExceeded, Storage
from app.transport import IncomingAttachment

HttpPost = Callable[
    [str, Mapping[str, str], Mapping[str, Any], float],
    Awaitable[tuple[int, Mapping[str, Any], Mapping[str, str]]],
]


class OpenAIProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.details = dict(details or {})


class OpenAIAuthenticationError(OpenAIProviderError):
    pass


class OpenAIPermissionError(OpenAIProviderError):
    pass


class OpenAIInputTooLong(OpenAIProviderError):
    pass


class OpenAIQuotaError(OpenAIProviderError):
    pass


class OpenAITransientError(OpenAIProviderError):
    pass


_SECRET_PATTERN = re.compile(r"(?i)(?:bearer\s+)?sk-[a-z0-9_-]{8,}")


def safe_provider_error_details(
    status: int, payload: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Extract provider diagnostics without returning metadata, prompts, or secrets."""
    source = payload if isinstance(payload, Mapping) else {}
    error = source.get("error")
    error = error if isinstance(error, Mapping) else {}

    def clean(value: Any, limit: int = 240) -> str | None:
        if not isinstance(value, (str, int, float)):
            return None
        text = " ".join(str(value).split())
        text = _SECRET_PATTERN.sub("[redacted]", text)
        return text[:limit] or None

    code = clean(error.get("code") or source.get("code"), 80)
    error_type = clean(
        source.get("error_type") or error.get("type") or error.get("error_type"), 80
    )
    message = clean(error.get("message") or source.get("message"))
    return {
        key: value
        for key, value in {
            "status": int(status),
            "code": code,
            "type": error_type,
            "message": message,
        }.items()
        if value is not None
    }


def provider_error_summary(details: Mapping[str, Any]) -> str:
    status = details.get("status")
    code = details.get("code")
    if code is not None and str(code) == str(status):
        code = None
    label = code or details.get("type") or details.get("message")
    if label:
        return f"HTTP {status}: {label}" if status else str(label)
    return f"HTTP {status}" if status else "provider error"


@dataclass(frozen=True, slots=True)
class OpenAICompatibleConfig:
    base_url: str
    api_key_file: Path
    model: str
    provider_name: str = "openai"
    timeout_seconds: float = 60.0
    max_retries: int = 2
    max_output_tokens: int = 1200
    daily_request_limit: int = 50
    daily_token_limit: int = 100_000
    max_input_chars: int = 24_000
    timezone_offset_minutes: int = 300
    safety_identifier: str = "max_owner"
    reasoning_effort: str = "low"
    verbosity: str = "medium"
    source: str = "max"
    transcription_model: str = "gpt-4o-mini-transcribe"
    max_attachment_bytes: int = 20 * 1024 * 1024
    instructions: str = (
        "Ты личный AI-ассистент владельца. Отвечай по-русски, если пользователь "
        "не попросил иначе. Будь практичным и не утверждай, что выполнил внешнее "
        "действие, если у тебя нет соответствующего инструмента."
    )

    def read_api_key(self) -> str:
        key = self.api_key_file.read_text(encoding="utf-8").strip()
        if not key:
            raise OpenAIAuthenticationError("LLM API key file is empty")
        return key


class OpenAICompatibleProvider:
    def __init__(
        self,
        config: OpenAICompatibleConfig,
        storage: Storage,
        *,
        http_post: HttpPost | None = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self._http_post = http_post or self._default_http_post
        self.logger = logging.getLogger(
            f"max_ai_assistant.llm.{self.config.provider_name}"
        )

    def _today(self) -> str:
        local_timezone = timezone(timedelta(minutes=self.config.timezone_offset_minutes))
        return datetime.now(local_timezone).date().isoformat()

    def _prepare_input(
        self,
        messages: Sequence[tuple[str, str]],
        attachments: Sequence[IncomingAttachment] = (),
    ) -> list[dict[str, Any]]:
        if messages and len(messages[-1][1]) > self.config.max_input_chars:
            raise OpenAIInputTooLong("latest message exceeds input limit")
        selected: list[tuple[str, str]] = []
        used = 0
        for role, content in reversed(messages):
            if role not in {"user", "assistant"}:
                continue
            if selected and used + len(content) > self.config.max_input_chars:
                break
            selected.append((role, content))
            used += len(content)
        prepared: list[dict[str, Any]] = [
            {"role": role, "content": content}
            for role, content in reversed(selected)
        ]
        if attachments:
            for item in reversed(prepared):
                if item["role"] != "user":
                    continue
                content: list[dict[str, str]] = [
                    {"type": "input_text", "text": str(item["content"])}
                ]
                for attachment in attachments:
                    if attachment.url is None:
                        continue
                    if attachment.kind == "image":
                        content.append(
                            {"type": "input_image", "image_url": attachment.url}
                        )
                    elif attachment.kind == "file":
                        file_input = {
                            "type": "input_file",
                            "filename": attachment.filename,
                        }
                        if attachment.url.startswith("data:"):
                            file_input["file_data"] = attachment.url
                        else:
                            file_input["file_url"] = attachment.url
                        content.append(file_input)
                item["content"] = content
                break
        return prepared

    async def complete(
        self,
        messages: Sequence[tuple[str, str]],
        *,
        attachments: Sequence[IncomingAttachment] = (),
    ) -> str:
        started = time.monotonic()
        try:
            prepared_messages, prepared_attachments = await self._prepare_audio(
                messages, attachments
            )
            prepared_attachments = await self._prepare_files(prepared_attachments)
            text, input_tokens, output_tokens, model, cost = await self._complete(
                prepared_messages, prepared_attachments
            )
        except Exception as exc:
            self.storage.record_llm_event(
                provider=self.config.provider_name,
                model=self.config.model,
                source=self.config.source,
                status="error",
                latency_ms=int((time.monotonic() - started) * 1000),
                error_code=type(exc).__name__,
            )
            raise
        self.storage.record_llm_event(
            provider=self.config.provider_name,
            model=model,
            source=self.config.source,
            status="success",
            latency_ms=int((time.monotonic() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
        )
        return text

    async def _prepare_audio(
        self,
        messages: Sequence[tuple[str, str]],
        attachments: Sequence[IncomingAttachment],
    ) -> tuple[list[tuple[str, str]], tuple[IncomingAttachment, ...]]:
        audio = [item for item in attachments if item.kind == "audio"]
        remaining = tuple(item for item in attachments if item.kind != "audio")
        if not audio:
            return list(messages), remaining
        transcripts: list[str] = []
        for item in audio:
            transcript = await self._transcribe_audio(item)
            transcripts.append(f"[Транскрипция {item.filename}]\n{transcript}")
        prepared = list(messages)
        for index in range(len(prepared) - 1, -1, -1):
            role, content = prepared[index]
            if role == "user":
                prepared[index] = (role, "\n\n".join([content, *transcripts]))
                break
        return prepared, remaining

    async def _prepare_files(
        self, attachments: Sequence[IncomingAttachment]
    ) -> tuple[IncomingAttachment, ...]:
        prepared: list[IncomingAttachment] = []
        for attachment in attachments:
            if attachment.kind != "file":
                prepared.append(attachment)
                continue
            payload = await self._download_attachment(attachment)
            content_type = (
                mimetypes.guess_type(attachment.filename)[0]
                or "application/octet-stream"
            )
            encoded = base64.b64encode(payload).decode("ascii")
            prepared.append(
                replace(
                    attachment,
                    url=f"data:{content_type};base64,{encoded}",
                )
            )
        return tuple(prepared)

    async def _download_attachment(self, attachment: IncomingAttachment) -> bytes:
        if not attachment.url:
            raise OpenAIProviderError("File attachment URL is unavailable")
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(attachment.url) as download:
                    if download.status < 200 or download.status >= 300:
                        raise OpenAIProviderError("Unable to download MAX attachment")
                    payload = bytearray()
                    async for chunk in download.content.iter_chunked(64 * 1024):
                        payload.extend(chunk)
                        if len(payload) > self.config.max_attachment_bytes:
                            raise OpenAIInputTooLong(
                                "File attachment exceeds size limit"
                            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise OpenAITransientError("MAX attachment download failed") from exc
        return bytes(payload)

    async def _transcribe_audio(self, attachment: IncomingAttachment) -> str:
        if self.config.provider_name != "openai":
            raise OpenAIProviderError(
                "Audio transcription requires the official OpenAI provider"
            )
        if not attachment.url:
            raise OpenAIProviderError("Audio attachment URL is unavailable")
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
        headers = {"Authorization": f"Bearer {self.config.read_api_key()}"}
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(attachment.url) as download:
                    if download.status < 200 or download.status >= 300:
                        raise OpenAIProviderError("Unable to download MAX audio")
                    payload = bytearray()
                    async for chunk in download.content.iter_chunked(64 * 1024):
                        payload.extend(chunk)
                        if len(payload) > self.config.max_attachment_bytes:
                            raise OpenAIInputTooLong("Audio attachment exceeds size limit")
                form = aiohttp.FormData()
                form.add_field("model", self.config.transcription_model)
                form.add_field(
                    "file",
                    bytes(payload),
                    filename=attachment.filename,
                    content_type=mimetypes.guess_type(attachment.filename)[0]
                    or "application/octet-stream",
                )
                url = self.config.base_url.rstrip("/") + "/audio/transcriptions"
                async with session.post(url, headers=headers, data=form) as response:
                    try:
                        result = await response.json(content_type=None)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        result = {}
                    if response.status < 200 or response.status >= 300:
                        details = safe_provider_error_details(response.status, result)
                        raise OpenAIProviderError(
                            f"Audio transcription failed ({provider_error_summary(details)})",
                            status=response.status,
                            details=details,
                        )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise OpenAITransientError("Audio transcription request failed") from exc
        text = str(result.get("text") or "").strip()
        if not text:
            raise OpenAIProviderError("Audio transcription is empty")
        return text

    async def _complete(
        self,
        messages: Sequence[tuple[str, str]],
        attachments: Sequence[IncomingAttachment] = (),
    ) -> tuple[str, int, int, str, float | None]:
        api_key = self.config.read_api_key()
        prepared = self._prepare_input(messages, attachments)
        if not prepared:
            raise OpenAIProviderError("LLM input is empty")

        day = self._today()
        self.storage.reserve_daily_request(
            day,
            self.config.daily_request_limit,
            self.config.daily_token_limit,
        )
        body: dict[str, Any] = {
            "model": self.config.model,
            "instructions": self.config.instructions,
            "input": prepared,
            "store": False,
            "max_output_tokens": self.config.max_output_tokens,
            "reasoning": {"effort": self.config.reasoning_effort},
        }
        if self.config.provider_name == "openai":
            body["text"] = {"verbosity": self.config.verbosity}
            body["safety_identifier"] = self.config.safety_identifier
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if self.config.provider_name == "openrouter":
            headers["X-OpenRouter-Title"] = "MAX AI Assistant"
        url = self.config.base_url.rstrip("/") + "/responses"

        for attempt in range(self.config.max_retries + 1):
            try:
                status, payload, response_headers = await self._http_post(
                    url, headers, body, self.config.timeout_seconds
                )
            except (OSError, TimeoutError) as exc:
                if attempt >= self.config.max_retries:
                    raise OpenAITransientError("LLM network request failed") from exc
                await asyncio.sleep(2**attempt)
                continue

            if 200 <= status < 300:
                text = self._extract_output_text(payload)
                usage = payload.get("usage") or {}
                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
                self.storage.record_token_usage(
                    day,
                    input_tokens,
                    output_tokens,
                )
                cost_value = usage.get("cost")
                try:
                    cost = float(cost_value) if cost_value is not None else None
                except (TypeError, ValueError):
                    cost = None
                return (
                    text,
                    input_tokens,
                    output_tokens,
                    str(payload.get("model") or self.config.model),
                    cost,
                )
            details = safe_provider_error_details(status, payload)
            summary = provider_error_summary(details)
            if status == 401:
                raise OpenAIAuthenticationError(
                    f"LLM authentication failed ({summary})",
                    status=status,
                    details=details,
                )
            if status == 403:
                raise OpenAIPermissionError(
                    f"LLM request is forbidden ({summary})",
                    status=status,
                    details=details,
                )
            if status == 402:
                raise OpenAIQuotaError(
                    f"LLM provider quota is unavailable ({summary})",
                    status=status,
                    details=details,
                )
            if status == 400:
                raise OpenAIProviderError(
                    f"LLM provider rejected the request ({summary})",
                    status=status,
                    details=details,
                )
            if status == 429:
                error = payload.get("error") or {}
                if error.get("code") == "insufficient_quota":
                    raise OpenAIQuotaError("LLM provider quota is unavailable")
            if status in {408, 409, 429} or status >= 500:
                if attempt >= self.config.max_retries:
                    raise OpenAITransientError("LLM provider is temporarily unavailable")
                retry_after = response_headers.get("Retry-After", "")
                try:
                    delay = min(10.0, max(0.5, float(retry_after)))
                except ValueError:
                    delay = float(2**attempt)
                await asyncio.sleep(delay)
                continue
            raise OpenAIProviderError(f"Unexpected LLM HTTP status {status}")
        raise OpenAITransientError("LLM retry loop exhausted")

    @staticmethod
    def _extract_output_text(payload: Mapping[str, Any]) -> str:
        parts: list[str] = []
        for item in payload.get("output") or []:
            if item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if content.get("type") == "output_text" and content.get("text"):
                    parts.append(str(content["text"]))
        text = "".join(parts).strip()
        if not text:
            raise OpenAIProviderError("LLM response did not contain output text")
        return text

    @staticmethod
    async def _default_http_post(
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
        timeout_seconds: float,
    ) -> tuple[int, Mapping[str, Any], Mapping[str, str]]:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url,
                    headers=dict(headers),
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                ) as response:
                    try:
                        payload = await response.json(content_type=None)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        payload = {}
                    return response.status, payload, dict(response.headers)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise OSError("LLM HTTP transport failed") from exc

    async def close(self) -> None:
        return None
