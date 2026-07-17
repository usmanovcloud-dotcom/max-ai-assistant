import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.dashboard import SecretStore, effective_settings, save_settings
from app.storage import Storage


class DashboardSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        env = {
            "APP_DATA_DIR": str(self.root / "data"),
            "LLM_API_KEY_FILE": str(self.root / "secrets" / "openrouter-api-key.txt"),
            "WEB_AUTOSTART_AI": "false",
        }
        with patch.dict(os.environ, env, clear=True):
            self.settings = Settings.from_env()
        self.storage = Storage(self.settings.database_path)
        self.storage.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_saved_settings_are_validated_and_applied(self) -> None:
        saved = save_settings(
            self.settings,
            self.storage,
            {"llm_model": "test/model", "llm_daily_limit": 25},
        )
        self.assertEqual(saved.llm_model, "test/model")
        self.assertEqual(effective_settings(self.settings, self.storage).llm_daily_limit, 25)
        with self.assertRaises(ValueError):
            save_settings(self.settings, self.storage, {"web_host": "0.0.0.0"})

    def test_secret_is_masked_and_never_returned(self) -> None:
        secrets = SecretStore(self.settings, self.storage)
        key = "sk-or-v1-" + "x" * 40
        secrets.save("openrouter", key)
        status = secrets.status("openrouter")
        self.assertTrue(status["configured"])
        self.assertNotIn(key, str(status))
        self.assertTrue(secrets.delete("openrouter"))
        self.assertFalse(secrets.status("openrouter")["configured"])


class _FakeResponse:
    status = 403

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, **kwargs):
        return {
            "error": {
                "code": "permission_denied",
                "message": "Key sk-or-v1-secretvalue cannot use this endpoint",
                "metadata": {"flagged_input": "must never be returned"},
            },
            "error_type": "permission_denied",
        }


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, *args, **kwargs):
        return _FakeResponse()


class DashboardProviderDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp.name)
        env = {
            "APP_DATA_DIR": str(root / "data"),
            "LLM_API_KEY_FILE": str(root / "secrets" / "openrouter-api-key.txt"),
            "WEB_AUTOSTART_AI": "false",
        }
        with patch.dict(os.environ, env, clear=True):
            self.settings = Settings.from_env()
        self.storage = Storage(self.settings.database_path)
        self.storage.initialize()

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_key_test_returns_sanitized_provider_reason(self) -> None:
        secrets = SecretStore(self.settings, self.storage)
        secrets.save("openrouter", "sk-or-v1-" + "x" * 40)
        with patch("app.dashboard.aiohttp.ClientSession", return_value=_FakeSession()):
            result = await secrets.test("openrouter")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 403)
        self.assertEqual(result["error"]["type"], "permission_denied")
        self.assertIn("[redacted]", result["error"]["message"])
        self.assertNotIn("secretvalue", str(result))
        self.assertNotIn("flagged_input", str(result))
