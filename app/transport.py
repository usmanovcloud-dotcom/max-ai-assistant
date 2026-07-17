from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    message_id: str
    chat_id: str
    sender_id: str | None
    text: str
    is_outgoing: bool = False
    is_direct: bool = False


@runtime_checkable
class MaxTransport(Protocol):
    """Replaceable boundary around PyMax or a future fallback transport."""

    async def messages(self) -> AsyncIterator[IncomingMessage]: ...

    async def send_text(self, chat_id: str, text: str) -> None: ...

    async def close(self) -> None: ...
