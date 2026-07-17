from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from app.chunking import split_text
from app.pairing import ClaimResult, OwnerGate, PairingManager
from app.queue import PerChatQueue
from app.storage import ReservationState, Storage
from app.transport import IncomingAttachment, IncomingMessage, MaxTransport

Responder = Callable[
    [str, list[tuple[str, str]], tuple[IncomingAttachment, ...]], Awaitable[str]
]


def _history_text(text: str, attachments: tuple[IncomingAttachment, ...]) -> str:
    prompt = text.strip() or "Проанализируй прикреплённый файл."
    labels_by_kind = {"image": "Изображение", "audio": "Аудио", "file": "Файл"}
    labels = [
        f"[{labels_by_kind.get(item.kind, 'Вложение')}: {item.filename}]"
        for item in attachments
    ]
    return "\n".join([prompt, *labels])


@dataclass(slots=True)
class AssistantCore:
    storage: Storage
    transport: MaxTransport
    responder: Responder
    pairing: PairingManager
    owner_gate: OwnerGate
    queue: PerChatQueue
    max_message_chars: int = 3500
    history_limit: int = 30
    logger: logging.Logger = logging.getLogger("max_ai_assistant.core")

    async def handle(self, message: IncomingMessage) -> str:
        claim_result = self.pairing.try_claim(message)
        if claim_result is ClaimResult.CLAIMED:
            await self.transport.send_text(message.chat_id, "Аккаунт владельца привязан.")
            return "claimed"
        if claim_result is not ClaimResult.NOT_A_CLAIM:
            return f"claim_{claim_result.value}"
        if not self.owner_gate.authorize(message):
            return "ignored"

        async with self.queue.acquire(message.chat_id):
            reservation = self.storage.reserve_message(message.message_id, message.chat_id)
            if reservation.state in {ReservationState.PROCESSING, ReservationState.SENT}:
                return "duplicate"

            if reservation.state is ReservationState.RESPONSE_READY:
                response = reservation.response_text
                next_chunk_index = reservation.next_chunk_index
                if response is None:
                    raise RuntimeError("ready response is missing")
            else:
                next_chunk_index = 0
                effective_text = message.text.strip() or "Проанализируй прикреплённый файл."
                command = effective_text.lower()
                is_command = not message.attachments and command in {"/new", "/help", "/status"}
                if reservation.state is ReservationState.NEW and is_command and command == "/new":
                    self.storage.new_conversation(message.chat_id)
                elif reservation.state is ReservationState.NEW and not is_command:
                    self.storage.append_message(
                        message.chat_id,
                        "user",
                        _history_text(effective_text, message.attachments),
                    )
                try:
                    history = self.storage.get_history(
                        message.chat_id, limit=self.history_limit
                    )
                    if reservation.state is ReservationState.NEW and not is_command:
                        try:
                            await self.transport.send_feedback(
                                message.chat_id, "Готовлю ответ…"
                            )
                        except Exception as exc:
                            self.logger.warning(
                                "Unable to send response feedback message_id=%s error=%s",
                                message.message_id,
                                type(exc).__name__,
                            )
                    response = await self.responder(
                        effective_text, history, message.attachments
                    )
                    self.storage.store_response(message.message_id, response)
                except Exception as exc:
                    self.storage.mark_failed(message.message_id, type(exc).__name__)
                    self.logger.error("Response generation failed for message_id=%s", message.message_id)
                    raise

            chunks = split_text(response, self.max_message_chars)
            for chunk_index, chunk in enumerate(chunks):
                if chunk_index < next_chunk_index:
                    continue
                await self.transport.send_text(message.chat_id, chunk)
                self.storage.mark_chunk_sent(message.message_id, chunk_index)
            self.storage.mark_sent(message.message_id)
            if message.attachments or message.text.strip().lower() not in {
                "/new",
                "/help",
                "/status",
            }:
                self.storage.append_message(message.chat_id, "assistant", response)
            return "sent"
