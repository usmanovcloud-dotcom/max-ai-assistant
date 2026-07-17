import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.dashboard import SecretStore
from app.provider_info import ProviderInfoService
from app.storage import Storage


class ProviderInfoTests(unittest.IsolatedAsyncioTestCase):
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
            self.settings = Settings.from_env()
        self.storage = Storage(self.settings.database_path)
        self.storage.initialize()
        self.secrets = SecretStore(self.settings, self.storage)
        self.service = ProviderInfoService(self.settings, self.storage, self.secrets)

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_openai_models_are_filtered_for_text_responses(self) -> None:
        self.secrets.save("openai", "sk-" + "a" * 40)
        self.service._get_json = AsyncMock(
            return_value={
                "data": [
                    {"id": "gpt-5.6-luna", "owned_by": "openai"},
                    {"id": "text-embedding-3-small", "owned_by": "openai"},
                    {"id": "gpt-realtime", "owned_by": "openai"},
                ]
            }
        )
        models = await self.service.models()
        self.assertEqual([item["id"] for item in models], ["gpt-5.6-luna"])

    async def test_openai_costs_use_separate_admin_key_and_budget(self) -> None:
        self.secrets.save("openai-admin", "sk-admin-" + "b" * 40)
        self.service._get_json = AsyncMock(
            return_value={
                "data": [
                    {"results": [{"amount": {"value": 2.5, "currency": "usd"}}]}
                ]
            }
        )
        account = await self.service.account("openai")
        self.assertEqual(account["budget"], 10.0)
        self.assertEqual(account["used"], 2.5)
        self.assertEqual(account["remaining_percent"], 75.0)

    async def test_openai_account_is_explicitly_unavailable_without_admin_key(self) -> None:
        account = await self.service.account("openai")
        self.assertFalse(account["available"])
        self.assertIn("Admin key", account["reason"])


class ContainerSettingsTests(unittest.TestCase):
    def test_container_can_bind_all_interfaces_only_when_explicit(self) -> None:
        with patch.dict(
            os.environ,
            {"WEB_HOST": "0.0.0.0", "CONTAINER_MODE": "true"},
            clear=True,
        ):
            self.assertTrue(Settings.from_env().container_mode)
        with patch.dict(os.environ, {"WEB_HOST": "0.0.0.0"}, clear=True):
            with self.assertRaisesRegex(ValueError, "loopback"):
                Settings.from_env()
