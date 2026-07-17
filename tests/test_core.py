import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator

from app.core import AssistantCore
from app.pairing import OwnerGate, PairingManager
from app.queue import PerChatQueue
from app.storage import ReservationState, Storage
from app.transport import IncomingAttachment, IncomingMessage


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.feedback: list[tuple[str, str]] = []
        self.fail_next_send = False
        self.fail_on_send_number: int | None = None
        self.send_attempts = 0

    async def messages(self) -> AsyncIterator[IncomingMessage]:
        if False:
            yield IncomingMessage("", "", None, "")

    async def send_text(self, chat_id: str, text: str) -> None:
        self.send_attempts += 1
        if self.fail_next_send:
            self.fail_next_send = False
            raise ConnectionError("simulated")
        if self.fail_on_send_number == self.send_attempts:
            raise ConnectionError("simulated")
        self.sent.append((chat_id, text))

    async def send_feedback(self, chat_id: str, text: str) -> None:
        self.feedback.append((chat_id, text))

    async def close(self) -> None:
        return None


class AssistantCoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.storage = Storage(Path(self.temp.name) / "test.sqlite3")
        self.storage.initialize()
        self.transport = FakeTransport()
        self.calls = 0

        async def responder(text, history, attachments) -> str:
            self.calls += 1
            return f"echo: {text}"

        pairing = PairingManager(self.storage)
        code = pairing.create_claim_code()
        pairing.try_claim(IncomingMessage("claim", "chat", "owner", f"/claim {code}", is_direct=True))
        self.core = AssistantCore(
            storage=self.storage,
            transport=self.transport,
            responder=responder,
            pairing=pairing,
            owner_gate=OwnerGate(self.storage),
            queue=PerChatQueue(),
            max_message_chars=10,
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_owner_message_is_processed_once_and_chunked(self) -> None:
        message = IncomingMessage("m1", "chat", "owner", "123456789", is_direct=True)
        self.assertEqual(await self.core.handle(message), "sent")
        self.assertEqual(await self.core.handle(message), "duplicate")
        self.assertEqual(self.calls, 1)
        self.assertEqual("".join(text for _, text in self.transport.sent), "echo: 123456789")
        self.assertEqual(self.transport.feedback, [("chat", "Готовлю ответ…")])

    async def test_other_sender_chat_and_outgoing_are_ignored(self) -> None:
        cases = [
            IncomingMessage("m1", "chat", "other", "text", is_direct=True),
            IncomingMessage("m2", "other-chat", "owner", "text", is_direct=True),
            IncomingMessage("m3", "chat", "owner", "text", is_outgoing=True, is_direct=True),
            IncomingMessage("m4", "chat", None, "text", is_direct=True),
            IncomingMessage("m5", "chat", "owner", "text", is_direct=False),
        ]
        for message in cases:
            self.assertEqual(await self.core.handle(message), "ignored")
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.transport.sent, [])

    async def test_send_retry_reuses_cached_response(self) -> None:
        message = IncomingMessage("m1", "chat", "owner", "hello", is_direct=True)
        self.transport.fail_next_send = True
        with self.assertRaises(ConnectionError):
            await self.core.handle(message)
        reservation = self.storage.reserve_message("m1", "chat")
        self.assertEqual(reservation.state, ReservationState.RESPONSE_READY)

        self.assertEqual(await self.core.handle(message), "sent")
        self.assertEqual(self.calls, 1)
        self.assertEqual("".join(text for _, text in self.transport.sent), "echo: hello")

    async def test_failed_generation_is_retryable_without_duplicate_input_history(self) -> None:
        attempts = 0

        async def flaky(text, history, attachments) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError("simulated")
            return "ok"

        self.core.responder = flaky
        message = IncomingMessage("m1", "chat", "owner", "hello", is_direct=True)
        with self.assertRaises(TimeoutError):
            await self.core.handle(message)
        self.assertEqual(await self.core.handle(message), "sent")
        history = self.storage.get_history("chat")
        self.assertEqual(history, [("user", "hello"), ("assistant", "ok")])

    async def test_partial_chunk_retry_resumes_after_sent_chunk(self) -> None:
        message = IncomingMessage("m1", "chat", "owner", "123456789", is_direct=True)
        self.transport.fail_on_send_number = 2
        with self.assertRaises(ConnectionError):
            await self.core.handle(message)
        first_chunk = self.transport.sent.copy()
        self.assertEqual(len(first_chunk), 1)

        self.transport.fail_on_send_number = None
        self.assertEqual(await self.core.handle(message), "sent")
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.transport.sent[:1], first_chunk)
        self.assertEqual("".join(text for _, text in self.transport.sent), "echo: 123456789")

    async def test_new_command_rotates_history_without_recording_command(self) -> None:
        self.storage.append_message("chat", "user", "old")

        async def command_responder(text, history, attachments) -> str:
            self.assertEqual(history, [])
            return "new conversation"

        self.core.responder = command_responder
        message = IncomingMessage("m-new", "chat", "owner", "/new", is_direct=True)
        self.assertEqual(await self.core.handle(message), "sent")
        self.assertEqual(self.storage.get_history("chat"), [])

    async def test_attachment_without_caption_gets_default_prompt_and_safe_history(self) -> None:
        captured = {}

        async def responder(text, history, attachments) -> str:
            captured.update(text=text, history=history, attachments=attachments)
            return "done"

        self.core.responder = responder
        attachment = IncomingAttachment(
            "file", "report.pdf", "https://max.example/signed-secret"
        )
        message = IncomingMessage(
            "m-file", "chat", "owner", "", is_direct=True, attachments=(attachment,)
        )
        self.assertEqual(await self.core.handle(message), "sent")
        self.assertEqual(captured["text"], "Проанализируй прикреплённый файл.")
        self.assertEqual(captured["attachments"], (attachment,))
        self.assertIn("report.pdf", captured["history"][-1][1])
        self.assertNotIn("signed-secret", str(captured["history"]))
