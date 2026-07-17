from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.config import Settings
from app.dashboard import effective_settings
from app.monitoring import RingLogHandler
from app.n8n import ForwardingResponder, N8nForwarder
from app.runtime import make_llm_responder, make_pymax_transport, run_ai, run_gate0
from app.pairing import PairingManager
from app.storage import Storage


class AssistantSupervisor:
    STOP_TIMEOUT_SECONDS = 5.0

    def __init__(
        self,
        base_settings: Settings,
        storage: Storage,
        logs: RingLogHandler,
    ) -> None:
        self.base_settings = base_settings
        self.storage = storage
        self.logs = logs
        self.logger = logging.getLogger("max_ai_assistant.supervisor")
        self._task: asyncio.Task[None] | None = None
        self._transport: Any | None = None
        self._state = "stopped"
        self._started_at: int | None = None
        self._last_error: str | None = None
        self._operation_lock = asyncio.Lock()
        self._chat_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.n8n = N8nForwarder(base_settings, storage)

    def settings(self) -> Settings:
        return effective_settings(self.base_settings, self.storage)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        async with self._operation_lock:
            if self.running:
                return
            settings = self.settings()
            transport = make_pymax_transport(settings)
            pairing = PairingManager(self.storage, settings.claim_ttl_seconds)
            self._transport = transport
            self._state = "starting"
            self._last_error = None
            self._started_at = int(time.time())
            if self.storage.get_owner() is None:
                code = pairing.create_claim_code()
                settings.claim_command_path.write_text(
                    f"/claim {code}\n", encoding="utf-8"
                )
                try:
                    settings.claim_command_path.chmod(0o600)
                except OSError:
                    pass
                self._task = asyncio.create_task(
                    self._run_pairing(transport, pairing, settings),
                    name="max-pairing-runtime",
                )
                self.storage.add_audit("pairing_started")
                self.logger.info("MAX pairing runtime start requested")
                return
            if not settings.llm_api_key_file.is_file():
                self._transport = None
                self._state = "waiting_api_key"
                raise RuntimeError("API key is not configured")
            base_responder = make_llm_responder(settings, self.storage, source="max")
            responder = ForwardingResponder(base_responder, self.n8n, "max")
            self._task = asyncio.create_task(
                self._run(transport, responder, pairing, settings),
                name="max-ai-runtime",
            )
            self.storage.add_audit("assistant_started")
            self.logger.info("Assistant runtime start requested")

    async def _run_pairing(
        self, transport: Any, pairing: PairingManager, settings: Settings
    ) -> None:
        self._state = "pairing"
        paired = False
        try:
            await run_gate0(
                self.storage,
                transport,
                pairing,
                settings.claim_command_path,
                settings.max_message_chars,
            )
            paired = self.storage.get_owner() is not None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = type(exc).__name__
            self._state = "error"
            self.logger.error("MAX pairing stopped error=%s", type(exc).__name__)
        finally:
            if self._state != "error":
                self._state = "stopped"
        if paired and self.settings().llm_api_key_file.is_file():
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.create_task(self.start())
            )

    async def _run(self, transport: Any, responder: Any, pairing: Any, settings: Settings) -> None:
        self._state = "running"
        try:
            await run_ai(
                self.storage,
                transport,
                responder,
                pairing,
                settings.claim_command_path,
                settings.max_message_chars,
                settings.llm_history_messages,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = type(exc).__name__
            self._state = "error"
            self.logger.error("Assistant runtime stopped error=%s", type(exc).__name__)
        finally:
            if self._state != "error":
                self._state = "stopped"

    async def stop(self) -> None:
        async with self._operation_lock:
            task = self._task
            if task is None or task.done():
                self._task = None
                self._transport = None
                self._state = "stopped"
                return
            self._state = "stopping"
            task.cancel()
            done, _ = await asyncio.wait(
                {task}, timeout=self.STOP_TIMEOUT_SECONDS
            )
            if task in done:
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    self.logger.warning(
                        "Runtime ended during stop error=%s", type(exc).__name__
                    )
            else:
                self.logger.warning(
                    "Runtime stop timed out after %.1f seconds; continuing restart",
                    self.STOP_TIMEOUT_SECONDS,
                )
            self._task = None
            self._transport = None
            self._state = "stopped"
            self.storage.add_audit("assistant_stopped")
            self.logger.info("Assistant runtime stopped")

    async def restart(self) -> None:
        await self.stop()
        await self.start()
        self.storage.add_audit("assistant_restarted")

    async def send_to_max(self, text: str) -> None:
        message = text.strip()
        if not message:
            raise ValueError("Message must not be empty")
        if len(message) > self.base_settings.max_message_chars:
            raise ValueError("Message is too long")
        owner = self.storage.get_owner()
        if owner is None:
            raise RuntimeError("Owner is not paired")
        if not self.running or self._transport is None:
            raise RuntimeError("Assistant runtime is stopped")
        await self._transport.send_text(owner.chat_id, message)
        self.storage.add_audit("max_message_sent", f"chars={len(message)}")

    async def chat(self, conversation_id: str, text: str) -> str:
        message = text.strip()
        if not message:
            raise ValueError("Message must not be empty")
        conversation = self.storage.get_web_conversation(conversation_id)
        if conversation is None:
            raise KeyError("Conversation not found")
        settings = self.settings()
        if len(message) > settings.llm_max_input_chars:
            raise ValueError("Message is too long")
        if not settings.llm_api_key_file.is_file():
            raise RuntimeError("API key is not configured")
        async with self._chat_locks[conversation_id]:
            self.storage.append_message(conversation_id, "user", message)
            if conversation.title == "Новый диалог":
                self.storage.touch_web_conversation(
                    conversation_id, title=message.replace("\n", " ")[:60]
                )
            else:
                self.storage.touch_web_conversation(conversation_id)
            history = self.storage.get_history(
                conversation_id, limit=settings.llm_history_messages
            )
            responder = make_llm_responder(settings, self.storage, source="web")
            try:
                answer = await responder(message, history)
            finally:
                await responder.provider.close()
            self.storage.append_message(conversation_id, "assistant", answer)
            self.storage.touch_web_conversation(conversation_id)
            self.n8n.schedule(
                source="web",
                provider=settings.llm_provider,
                model=settings.llm_model,
                prompt=message,
                response=answer,
            )
            return answer

    def status(self) -> dict[str, Any]:
        settings = self.settings()
        owner = self.storage.get_owner()
        qr: dict[str, Any] = {"phase": "unknown"}
        if settings.qr_status_path.is_file():
            try:
                loaded = json.loads(settings.qr_status_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    qr = loaded
            except (OSError, json.JSONDecodeError):
                qr = {"phase": "unreadable"}
        connected = bool(
            self._transport is not None
            and getattr(self._transport, "connected", False)
        )
        qr = dict(qr)
        qr["connected"] = connected
        if owner is None and settings.claim_command_path.is_file():
            try:
                claim_command = settings.claim_command_path.read_text(
                    encoding="utf-8"
                ).strip()
                if claim_command.startswith("/claim "):
                    qr["claim_command"] = claim_command
            except OSError:
                pass
        if qr.get("phase") == "authenticated" and not connected:
            qr["phase"] = "reconnecting" if self.running else "disconnected"
        return {
            "state": self._state,
            "running": self.running,
            "started_at": self._started_at,
            "uptime_seconds": int(time.time()) - self._started_at
            if self.running and self._started_at
            else 0,
            "last_error": self._last_error,
            "max": qr,
            "owner_paired": owner is not None,
            "owner_chat_id": owner.chat_id if owner else None,
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "queue": self.storage.get_runtime_counts(),
        }

    async def revoke_max_session(self) -> None:
        await self.stop()
        settings = self.settings()
        session_paths = (
            settings.max_session_path,
            Path(str(settings.max_session_path) + "-wal"),
            Path(str(settings.max_session_path) + "-shm"),
            settings.qr_path,
            settings.qr_status_path,
        )
        for path in session_paths:
            Path(path).unlink(missing_ok=True)
        self.storage.add_audit("max_session_revoked")
