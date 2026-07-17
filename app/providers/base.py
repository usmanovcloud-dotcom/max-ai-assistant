from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    async def complete(self, messages: Sequence[tuple[str, str]]) -> str: ...

    async def close(self) -> None: ...
