from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Literal, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class IncomingAttachment:
    kind: Literal["file", "image", "audio", "unsupported"]
    filename: str
    url: str | None = None
    size: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    message_id: str
    chat_id: str
    sender_id: str | None
    text: str
    is_outgoing: bool = False
    is_direct: bool = False
    attachments: tuple[IncomingAttachment, ...] = ()


@runtime_checkable
class MaxTransport(Protocol):
    """Replaceable boundary around PyMax or a future fallback transport."""

    async def messages(self) -> AsyncIterator[IncomingMessage]: ...

    async def send_text(self, chat_id: str, text: str) -> None: ...

    async def send_feedback(self, chat_id: str, text: str) -> None: ...

    async def close(self) -> None: ...
