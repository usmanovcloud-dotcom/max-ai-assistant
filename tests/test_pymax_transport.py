import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.pymax_transport import (
    LocalSecretPasswordProvider,
    LocalSvgQrHandler,
    PyMaxOptions,
    PyMaxTransport,
    SafeQrStatus,
)


class FakeClient:
    def __init__(self) -> None:
        self.me = SimpleNamespace(contact=SimpleNamespace(id=900))
        self.chats = [SimpleNamespace(id=10, type="DIALOG")]
        self.sent: list[tuple[int, str]] = []
        self.start_handlers = []
        self.message_handlers = []
        self.disconnect_handlers = []

    def on_start(self):
        return self.start_handlers.append

    def on_message(self):
        return self.message_handlers.append

    def on_disconnect(self):
        return self.disconnect_handlers.append

    async def get_chat(self, chat_id: int):
        return SimpleNamespace(id=chat_id, type="CHAT")

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    async def close(self) -> None:
        return None


class PyMaxTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp.name)
        self.client = FakeClient()
        self.transport = PyMaxTransport(
            PyMaxOptions(
                root / "session.sqlite3",
                root / "login.svg",
                root / "status.json",
                root / "2fa.txt",
            ),
            client_factory=lambda: self.client,
        )
        self.transport._ensure_client()

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_maps_verified_direct_message_and_own_sender(self) -> None:
        incoming = await self.transport._convert_message(
            SimpleNamespace(id=1, chat_id=10, sender=100, text="hello"), self.client
        )
        self.assertIsNotNone(incoming)
        self.assertTrue(incoming.is_direct)
        self.assertFalse(incoming.is_outgoing)
        self.assertEqual((incoming.message_id, incoming.chat_id, incoming.sender_id), ("1", "10", "100"))

        outgoing = await self.transport._convert_message(
            SimpleNamespace(id=2, chat_id=10, sender=900, text="answer"), self.client
        )
        self.assertTrue(outgoing.is_outgoing)

    async def test_unknown_group_is_not_marked_direct(self) -> None:
        incoming = await self.transport._convert_message(
            SimpleNamespace(id=1, chat_id=20, sender=100, text="hello"), self.client
        )
        self.assertFalse(incoming.is_direct)

    async def test_incomplete_and_non_text_events_are_ignored(self) -> None:
        missing_sender = await self.transport._convert_message(
            SimpleNamespace(id=1, chat_id=10, sender=None, text="hello"), self.client
        )
        empty = await self.transport._convert_message(
            SimpleNamespace(id=2, chat_id=10, sender=100, text=""), self.client
        )
        self.assertIsNone(missing_sender)
        self.assertIsNone(empty)

    async def test_send_converts_persisted_chat_id_to_integer(self) -> None:
        await self.transport.send_text("10", "hello")
        self.assertEqual(self.client.sent, [(10, "hello")])

    async def test_connection_state_tracks_start_disconnect_and_close(self) -> None:
        self.assertFalse(self.transport.connected)
        await self.client.start_handlers[0](self.client)
        self.assertTrue(self.transport.connected)

        await self.client.disconnect_handlers[0](ConnectionError(), True, 1.0)
        self.assertFalse(self.transport.connected)
        status = json.loads(
            self.transport.options.status_path.read_text(encoding="utf-8")
        )
        self.assertEqual(status["phase"], "reconnecting")

        await self.transport.close()
        self.assertFalse(self.transport.connected)
        status = json.loads(
            self.transport.options.status_path.read_text(encoding="utf-8")
        )
        self.assertEqual(status["phase"], "stopped")

    async def test_close_is_bounded_when_client_cleanup_stalls(self) -> None:
        never = asyncio.Event()

        async def stalled_close() -> None:
            await never.wait()

        self.client.close = stalled_close
        self.transport.CLOSE_TIMEOUT_SECONDS = 0.01

        await asyncio.wait_for(self.transport.close(), timeout=0.1)

        self.assertFalse(self.transport.connected)

    async def test_svg_qr_is_written_locally(self) -> None:
        path = Path(self.temp.name) / "qr" / "login.svg"
        await LocalSvgQrHandler(path).show_qr("https://example.invalid/secret")
        self.assertTrue(path.exists())
        self.assertIn(b"<svg", path.read_bytes())

    async def test_2fa_secret_is_consumed_once_and_deleted(self) -> None:
        root = Path(self.temp.name)
        secret_path = root / "2fa.txt"
        secret_path.write_text("local-password\n", encoding="utf-8")
        provider = LocalSecretPasswordProvider(
            secret_path, SafeQrStatus(root / "status.json"), timeout_seconds=1
        )
        self.assertEqual(await provider.get_password(), "local-password")
        self.assertFalse(secret_path.exists())


class PyMaxHardeningTests(unittest.TestCase):
    def test_default_client_disables_telemetry_and_uses_fixed_web_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = PyMaxTransport(
                PyMaxOptions(
                    root / "session.sqlite3",
                    root / "login.svg",
                    root / "status.json",
                    root / "2fa.txt",
                )
            )
            client = transport._ensure_client()
            self.assertFalse(client.extra_config.telemetry)
            self.assertTrue(client.extra_config.reconnect)
            user_agent = client.extra_config.user_agent
            self.assertEqual(user_agent.timezone, "Asia/Yekaterinburg")
            self.assertEqual(user_agent.locale, "ru")
            self.assertEqual(user_agent.app_version, "26.5.5")
