import asyncio
import unittest

from app.queue import PerChatQueue


class PerChatQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_chat_is_sequential(self) -> None:
        queue = PerChatQueue()
        active = 0
        maximum = 0

        async def work() -> None:
            nonlocal active, maximum
            async with queue.acquire("chat"):
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(*(work() for _ in range(4)))
        self.assertEqual(maximum, 1)

    async def test_different_chats_can_overlap(self) -> None:
        queue = PerChatQueue()
        both_active = asyncio.Event()
        active = 0

        async def work(chat_id: str) -> None:
            nonlocal active
            async with queue.acquire(chat_id):
                active += 1
                if active == 2:
                    both_active.set()
                await asyncio.wait_for(both_active.wait(), timeout=1)
                active -= 1

        await asyncio.gather(work("one"), work("two"))
