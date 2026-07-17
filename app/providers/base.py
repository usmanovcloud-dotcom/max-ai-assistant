from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from app.transport import IncomingAttachment


@runtime_checkable
class LLMProvider(Protocol):
    async def complete(
        self,
        messages: Sequence[tuple[str, str]],
        *,
        attachments: Sequence[IncomingAttachment] = (),
    ) -> str: ...

    async def close(self) -> None: ...
