from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlparse

from app.transport import IncomingAttachment, IncomingMessage


SUPPORTED_FILE_EXTENSIONS = {
    ".c", ".cpp", ".css", ".csv", ".doc", ".docx", ".go", ".html",
    ".java", ".js", ".json", ".md", ".odt", ".pdf", ".php", ".ppt",
    ".pptx", ".py", ".rb", ".rs", ".rtf", ".sh", ".sql", ".ts", ".tsv",
    ".txt", ".xls", ".xlsx", ".xml", ".yaml", ".yml",
}
SUPPORTED_AUDIO_EXTENSIONS = {
    ".aac", ".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga",
    ".oga", ".ogg", ".opus", ".wav", ".webm",
}


@dataclass(frozen=True, slots=True)
class PyMaxOptions:
    session_path: Path
    qr_path: Path
    status_path: Path
    two_factor_secret_path: Path
    log_level: str = "INFO"
    reconnect_delay: float = 2.0
    locale: str = "ru"
    timezone: str = "Asia/Yekaterinburg"
    header_user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    )
    max_attachment_count: int = 5
    max_attachment_bytes: int = 20 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _TransportStopped:
    error: BaseException | None = None


class LocalSvgQrHandler:
    """Writes only the QR image; the embedded login URL is never logged."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def show_qr(self, qr_url: str) -> None:
        await asyncio.to_thread(self._write_svg, qr_url)

    def _write_svg(self, qr_url: str) -> None:
        import qrcode
        from qrcode.image.svg import SvgPathImage

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        image = qrcode.make(qr_url, image_factory=SvgPathImage, border=4)
        with temporary.open("wb") as output:
            image.save(output)
        os.replace(temporary, self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass


class SafeQrStatus:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, phase: str, **details: object) -> None:
        import time

        payload = {"phase": phase, "updated_at": int(time.time()), **details}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.path)


class LocalSecretPasswordProvider:
    def __init__(
        self,
        path: Path,
        status: SafeQrStatus,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.path = path
        self.status = status
        self.timeout_seconds = timeout_seconds

    async def get_password(self, hint: str | None = None) -> str:
        import time

        self.status.write("2fa_required", hint_present=bool(hint))
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if self.path.exists():
                password = self.path.read_text(encoding="utf-8").strip()
                self.path.unlink(missing_ok=True)
                if password:
                    self.status.write("2fa_received")
                    return password
            await asyncio.sleep(0.5)
        self.status.write("2fa_timeout")
        raise TimeoutError("Local 2FA secret was not provided in time")


def _build_observable_qr_flow(
    qr_handler: object,
    status: SafeQrStatus,
    two_factor_secret_path: Path,
) -> object:
    from pymax import QrAuthFlow

    class ObservableQrAuthFlow(QrAuthFlow):
        async def authenticate(self, app: object) -> object:
            status.write("requesting_qr")
            result = await super().authenticate(app)
            status.write("session_starting")
            return result

        async def _poll_qr(self, app: object, qr_info: object) -> bool:
            status.write("waiting_confirmation", expires_at=getattr(qr_info, "expires_at", None))
            confirmed = await super()._poll_qr(app, qr_info)
            status.write("qr_confirmed" if confirmed else "expired")
            return confirmed

    return ObservableQrAuthFlow(
        qr_handler,
        LocalSecretPasswordProvider(two_factor_secret_path, status),
    )


class PyMaxTransport:
    """PyMax 2.3.1 adapter with a narrow, testable transport boundary."""

    CLOSE_TIMEOUT_SECONDS = 2.0

    def __init__(
        self,
        options: PyMaxOptions,
        *,
        client_factory: Callable[[], Any] | None = None,
        queue_size: int = 100,
    ) -> None:
        self.options = options
        self._client_factory = client_factory
        self._queue: asyncio.Queue[IncomingMessage | _TransportStopped] = asyncio.Queue(
            maxsize=queue_size
        )
        self._client: Any | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._closed = False
        self._connected = False
        self._direct_chats: dict[int, bool] = {}
        self.logger = logging.getLogger("max_ai_assistant.pymax")

    def _build_default_client(self) -> Any:
        from pymax import ExtraConfig, WebClient
        from pymax.api.session.enums import DeviceType
        from pymax.api.session.payloads import MobileUserAgentPayload

        user_agent = MobileUserAgentPayload(
            device_type=DeviceType.WEB,
            app_version="26.5.5",
            os_version="Linux",
            timezone=self.options.timezone,
            screen="1080x1920 1.0x",
            locale=self.options.locale,
            device_name="Chrome",
            device_locale=self.options.locale,
            header_user_agent=self.options.header_user_agent,
        )
        extra = ExtraConfig(
            reconnect=True,
            reconnect_delay=self.options.reconnect_delay,
            log_level=self.options.log_level,
            telemetry=False,
            user_agent=user_agent,
        )
        status = SafeQrStatus(self.options.status_path)
        return WebClient(
            session_name=self.options.session_path.name,
            work_dir=str(self.options.session_path.parent),
            extra_config=extra,
            auth_flow=_build_observable_qr_flow(
                LocalSvgQrHandler(self.options.qr_path),
                status,
                self.options.two_factor_secret_path,
            ),
        )

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        self.options.session_path.parent.mkdir(parents=True, exist_ok=True)
        factory = self._client_factory or self._build_default_client
        client = factory()

        @client.on_start()
        async def on_start(started_client: Any) -> None:
            self._connected = True
            self._cache_chat_types(started_client)
            self._remove_qr_file()
            SafeQrStatus(self.options.status_path).write("authenticated")
            self.logger.info("MAX session authenticated and transport started")

        @client.on_message()
        async def on_message(message: Any, event_client: Any) -> None:
            incoming = await self._convert_message(message, event_client)
            if incoming is not None:
                await self._queue.put(incoming)

        @client.on_disconnect()
        async def on_disconnect(error: BaseException, reconnect: bool, delay: float) -> None:
            self._connected = False
            SafeQrStatus(self.options.status_path).write(
                "reconnecting" if reconnect else "disconnected",
                error_type=type(error).__name__,
            )
            self.logger.warning(
                "MAX connection lost error=%s reconnect=%s delay=%s",
                type(error).__name__,
                reconnect,
                delay,
            )

        self._client = client
        return client

    @property
    def connected(self) -> bool:
        return self._connected and not self._closed

    def _cache_chat_types(self, client: Any) -> None:
        for chat in client.chats or []:
            self._direct_chats[int(chat.id)] = self._is_dialog(chat)

    @staticmethod
    def _is_dialog(chat: Any) -> bool:
        chat_type = getattr(chat, "type", None)
        value = getattr(chat_type, "value", chat_type)
        return value == "DIALOG"

    async def _is_direct_chat(self, client: Any, chat_id: int) -> bool:
        cached = self._direct_chats.get(chat_id)
        if cached is not None:
            return cached
        self._cache_chat_types(client)
        cached = self._direct_chats.get(chat_id)
        if cached is not None:
            return cached
        try:
            chat = await client.get_chat(chat_id)
        except Exception as exc:
            self.logger.warning(
                "Unable to verify chat type chat_id=%s error=%s",
                chat_id,
                type(exc).__name__,
            )
            return False
        direct = self._is_dialog(chat)
        self._direct_chats[chat_id] = direct
        return direct

    async def _convert_message(self, message: Any, client: Any) -> IncomingMessage | None:
        message_id = getattr(message, "id", None)
        chat_id = getattr(message, "chat_id", None)
        sender_id = getattr(message, "sender", None)
        text = getattr(message, "text", "")
        if message_id is None or chat_id is None or sender_id is None:
            self.logger.warning("Ignoring MAX event with incomplete identity fields")
            return None
        attachments = await self._convert_attachments(message, client)
        if not isinstance(text, str):
            text = ""
        if not text and not attachments:
            self.logger.info("Ignoring empty MAX message message_id=%s", message_id)
            return None

        me = getattr(client, "me", None)
        contact = getattr(me, "contact", None)
        own_id = getattr(contact, "id", None)
        return IncomingMessage(
            message_id=str(message_id),
            chat_id=str(chat_id),
            sender_id=str(sender_id),
            text=text,
            is_outgoing=own_id is not None and int(sender_id) == int(own_id),
            is_direct=await self._is_direct_chat(client, int(chat_id)),
            attachments=attachments,
        )

    async def _convert_attachments(
        self, message: Any, client: Any
    ) -> tuple[IncomingAttachment, ...]:
        from pymax.types.domain.attachments import (
            AudioAttachment,
            FileAttachment,
            PhotoAttachment,
        )

        source = tuple(getattr(message, "attaches", ()) or ())
        result: list[IncomingAttachment] = []
        for attachment in source[: self.options.max_attachment_count]:
            if isinstance(attachment, PhotoAttachment):
                url = self._safe_https_url(attachment.base_url)
                filename = f"photo-{attachment.photo_id}.jpg"
                result.append(
                    IncomingAttachment(
                        "image" if url else "unsupported",
                        filename,
                        url=url,
                        reason=None if url else "invalid_url",
                    )
                )
                continue
            if isinstance(attachment, AudioAttachment):
                url = self._safe_https_url(attachment.url)
                audio_id = attachment.audio_id or getattr(message, "id", "message")
                result.append(
                    IncomingAttachment(
                        "audio" if url else "unsupported",
                        f"voice-{audio_id}.ogg",
                        url=url,
                        reason=None if url else "invalid_url",
                    )
                )
                continue
            if isinstance(attachment, FileAttachment):
                filename = self._safe_filename(attachment.name, attachment.file_id)
                if attachment.size > self.options.max_attachment_bytes:
                    result.append(
                        IncomingAttachment(
                            "unsupported", filename, size=attachment.size, reason="too_large"
                        )
                    )
                    continue
                extension = Path(filename).suffix.lower()
                if extension not in SUPPORTED_FILE_EXTENSIONS | SUPPORTED_AUDIO_EXTENSIONS:
                    result.append(
                        IncomingAttachment(
                            "unsupported", filename, size=attachment.size, reason="file_type"
                        )
                    )
                    continue
                try:
                    download = await client.get_file_by_id(
                        int(message.chat_id), message.id, attachment.file_id
                    )
                except Exception as exc:
                    self.logger.warning(
                        "Unable to resolve MAX attachment message_id=%s error=%s",
                        message.id,
                        type(exc).__name__,
                    )
                    download = None
                url = self._safe_https_url(getattr(download, "url", None))
                unsafe = bool(getattr(download, "unsafe", False))
                result.append(
                    IncomingAttachment(
                        ("audio" if extension in SUPPORTED_AUDIO_EXTENSIONS else "file")
                        if url and not unsafe
                        else "unsupported",
                        filename,
                        url=url if not unsafe else None,
                        size=attachment.size,
                        reason=None if url and not unsafe else "unavailable",
                    )
                )
                continue
            result.append(
                IncomingAttachment(
                    "unsupported",
                    type(attachment).__name__.removesuffix("Attachment") or "attachment",
                    reason="attachment_type",
                )
            )
        if len(source) > self.options.max_attachment_count:
            result.append(
                IncomingAttachment(
                    "unsupported", "additional-attachments", reason="too_many"
                )
            )
        return tuple(result)

    @staticmethod
    def _safe_filename(name: str, file_id: int) -> str:
        filename = Path(str(name).replace("\\", "/")).name
        filename = "".join(char for char in filename if char.isprintable())[:200]
        return filename or f"file-{file_id}"

    @staticmethod
    def _safe_https_url(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        candidate = "https:" + value if value.startswith("//") else value
        parsed = urlparse(candidate)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            return None
        return candidate

    async def _run_client(self) -> None:
        error: BaseException | None = None
        try:
            await self._ensure_client().start()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            error = exc
            SafeQrStatus(self.options.status_path).write(
                "failed", error_type=type(exc).__name__
            )
        finally:
            self._connected = False
            if not self._closed:
                await self._queue.put(_TransportStopped(error))

    async def messages(self) -> AsyncIterator[IncomingMessage]:
        if self._run_task is not None:
            raise RuntimeError("PyMax transport is already running")
        self._run_task = asyncio.create_task(self._run_client(), name="pymax-client")
        while True:
            item = await self._queue.get()
            if isinstance(item, _TransportStopped):
                if item.error is not None:
                    raise item.error
                return
            yield item

    async def send_text(self, chat_id: str, text: str) -> None:
        client = self._ensure_client()
        await client.send_message(int(chat_id), text)

    async def send_feedback(self, chat_id: str, text: str) -> None:
        await self.send_text(chat_id, text)

    async def close(self) -> None:
        self._closed = True
        self._connected = False
        SafeQrStatus(self.options.status_path).write("stopped")
        client = self._client
        if client is not None:
            try:
                await asyncio.wait_for(
                    client.close(), timeout=self.CLOSE_TIMEOUT_SECONDS
                )
            except TimeoutError:
                self.logger.warning(
                    "MAX client close timed out after %.1f seconds",
                    self.CLOSE_TIMEOUT_SECONDS,
                )
        task = self._run_task
        if task is not None and not task.done():
            task.cancel()
            done, _ = await asyncio.wait(
                {task}, timeout=self.CLOSE_TIMEOUT_SECONDS
            )
            if task in done:
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    self.logger.warning(
                        "MAX client task ended during close error=%s",
                        type(exc).__name__,
                    )
            else:
                self.logger.warning(
                    "MAX client task close timed out after %.1f seconds",
                    self.CLOSE_TIMEOUT_SECONDS,
                )
        self._remove_qr_file()

    def _remove_qr_file(self) -> None:
        try:
            self.options.qr_path.unlink(missing_ok=True)
        except OSError:
            self.logger.warning("Unable to remove expired local QR file")
