from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Any

from app.transport import IncomingAttachment
from urllib.parse import urlsplit

import aiohttp

from app.config import Settings
from app.storage import Storage


class N8nForwarder:
    def __init__(self, base: Settings, storage: Storage) -> None:
        self.base = base
        self.storage = storage
        secret_dir = base.llm_api_key_file.parent
        self.url_path = secret_dir / "n8n-webhook-url.txt"
        self.token_path = secret_dir / "n8n-auth-token.txt"
        self.last_status: dict[str, Any] = {"state": "never"}
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def enabled(self) -> bool:
        return self.storage.get_model_setting("n8n_enabled") == "true"

    def status(self) -> dict[str, Any]:
        url = self._read(self.url_path)
        token = self._read(self.token_path)
        return {
            "enabled": self.enabled,
            "configured": bool(url),
            "url_masked": self._mask_url(url) if url else None,
            "auth_configured": bool(token),
            "last": self.last_status,
        }

    def configure(
        self,
        *,
        enabled: bool,
        url: str | None = None,
        token: str | None = None,
        clear_token: bool = False,
    ) -> dict[str, Any]:
        current_url = self._read(self.url_path)
        if url is not None and url.strip():
            self._validate_url(url.strip())
            self._write(self.url_path, url.strip())
            current_url = url.strip()
        if clear_token:
            self.token_path.unlink(missing_ok=True)
        elif token is not None and token.strip():
            self._write(self.token_path, token.strip())
        if enabled and not current_url:
            raise ValueError("n8n webhook URL is required before enabling")
        self.storage.set_model_setting("n8n_enabled", "true" if enabled else "false")
        self.storage.add_audit("n8n_configuration_updated", f"enabled={str(enabled).lower()}")
        return self.status()

    async def test(self) -> dict[str, Any]:
        url = self._read(self.url_path)
        if not url:
            raise ValueError("n8n webhook URL is not configured")
        await self._post(
            {
                "event_id": uuid.uuid4().hex,
                "event": "connection_test",
                "source": "dashboard",
                "created_at": int(time.time()),
            }
        )
        return self.status()

    def schedule(
        self,
        *,
        source: str,
        provider: str,
        model: str,
        prompt: str,
        response: str,
    ) -> None:
        if not self.enabled or not self._read(self.url_path):
            return
        task = asyncio.create_task(
            self.emit(
                source=source,
                provider=provider,
                model=model,
                prompt=prompt,
                response=response,
            ),
            name="n8n-forward",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def emit(
        self,
        *,
        source: str,
        provider: str,
        model: str,
        prompt: str,
        response: str,
    ) -> None:
        try:
            await self._post(
                {
                    "event_id": uuid.uuid4().hex,
                    "event": "assistant_response",
                    "source": source,
                    "provider": provider,
                    "model": model,
                    "prompt": prompt,
                    "response": response,
                    "created_at": int(time.time()),
                }
            )
        except Exception as exc:
            self.last_status = {
                "state": "error",
                "error": type(exc).__name__,
                "updated_at": int(time.time()),
            }
            self.storage.add_audit("n8n_delivery_failed", f"error={type(exc).__name__}")

    async def _post(self, payload: dict[str, Any]) -> None:
        url = self._read(self.url_path)
        if not url:
            raise ValueError("n8n webhook URL is not configured")
        headers = {"Content-Type": "application/json"}
        token = self._read(self.token_path)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    if not 200 <= response.status < 300:
                        raise RuntimeError(f"n8n webhook returned {response.status}")
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise RuntimeError("n8n webhook connection failed") from exc
        self.last_status = {"state": "ok", "updated_at": int(time.time())}
        self.storage.add_audit("n8n_delivery_succeeded", f"event={payload['event']}")

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("n8n webhook URL must use http or https")
        if parsed.username or parsed.password:
            raise ValueError("Credentials must not be embedded in n8n webhook URL")

    @staticmethod
    def _mask_url(url: str) -> str:
        parsed = urlsplit(url)
        tail = parsed.path.rstrip("/").split("/")[-1]
        masked_tail = f"…{tail[-4:]}" if tail else "…"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}/{masked_tail}"

    @staticmethod
    def _read(path: Path) -> str:
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""

    @staticmethod
    def _write(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(value, encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)


class ForwardingResponder:
    def __init__(self, responder: Any, forwarder: N8nForwarder, source: str) -> None:
        self.responder = responder
        self.forwarder = forwarder
        self.source = source
        self.provider = responder.provider

    async def __call__(
        self,
        text: str,
        history: list[tuple[str, str]],
        attachments: tuple[IncomingAttachment, ...] = (),
    ) -> str:
        answer = await self.responder(text, history, attachments)
        self.forwarder.schedule(
            source=self.source,
            provider=self.provider.config.provider_name,
            model=self.provider.config.model,
            prompt=text,
            response=answer,
        )
        return answer
