import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.n8n import N8nForwarder
from app.storage import Storage


class N8nForwarderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp.name)
        with patch.dict(
            os.environ,
            {
                "APP_DATA_DIR": str(root / "data"),
                "LLM_API_KEY_FILE": str(root / "secrets" / "openai-api-key.txt"),
                "WEB_AUTOSTART_AI": "false",
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.storage = Storage(settings.database_path)
        self.storage.initialize()
        self.forwarder = N8nForwarder(settings, self.storage)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_configuration_is_disabled_by_default_and_masks_url(self) -> None:
        self.assertFalse(self.forwarder.status()["enabled"])
        result = self.forwarder.configure(
            enabled=True,
            url="https://n8n.example/webhook/very-secret-id",
            token="secret-token",
        )
        self.assertTrue(result["enabled"])
        self.assertNotIn("very-secret-id", result["url_masked"])
        self.assertTrue(result["auth_configured"])

    def test_invalid_url_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.forwarder.configure(enabled=True, url="file:///tmp/webhook")

    async def test_connection_test_sends_only_test_event(self) -> None:
        self.forwarder.configure(
            enabled=False, url="https://n8n.example/webhook/test"
        )
        self.forwarder._post = AsyncMock()
        await self.forwarder.test()
        payload = self.forwarder._post.await_args.args[0]
        self.assertEqual(payload["event"], "connection_test")
        self.assertNotIn("prompt", payload)
