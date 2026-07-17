import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings


class SettingsTests(unittest.TestCase):
    def test_defaults_keep_telemetry_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"APP_DATA_DIR": directory}, clear=True
        ):
            settings = Settings.from_env()
        self.assertFalse(settings.pymax_telemetry_enabled)
        self.assertEqual(settings.database_path, Path(directory) / "assistant.sqlite3")
        self.assertEqual(settings.qr_path, Path(directory) / "login.svg")
        self.assertEqual(settings.llm_provider, "openai")
        self.assertEqual(settings.llm_base_url, "https://api.openai.com/v1")
        self.assertEqual(settings.llm_model, "gpt-5.6-luna")
        self.assertEqual(
            settings.llm_api_key_file,
            Path.cwd() / "secrets" / "openai-api-key.txt",
        )

    def test_telemetry_cannot_be_enabled(self) -> None:
        with patch.dict(os.environ, {"PYMAX_TELEMETRY_ENABLED": "true"}, clear=True):
            with self.assertRaisesRegex(ValueError, "telemetry"):
                Settings.from_env()

    def test_openai_profile_keeps_separate_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"APP_DATA_DIR": directory, "LLM_PROVIDER": "openai"},
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.llm_base_url, "https://api.openai.com/v1")
        self.assertEqual(settings.llm_model, "gpt-5.6-luna")
        self.assertEqual(
            settings.llm_api_key_file,
            Path.cwd() / "secrets" / "openai-api-key.txt",
        )

    def test_unknown_provider_is_rejected(self) -> None:
        with patch.dict(os.environ, {"LLM_PROVIDER": "unknown"}, clear=True):
            with self.assertRaisesRegex(ValueError, "LLM_PROVIDER"):
                Settings.from_env()
