import os
import tempfile
import unittest
from unittest.mock import patch

from app.config import Settings
from app.pairing import PairingManager
from app.runtime import make_llm_responder, make_pymax_transport
from app.storage import Storage
from app.transport import IncomingMessage


class RuntimeFactoryTests(unittest.TestCase):
    def test_provider_specific_timezones_are_mapped_to_correct_options(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory, patch.dict(
            os.environ, {"APP_DATA_DIR": directory}, clear=True
        ):
            settings = Settings.from_env()
            storage = Storage(settings.database_path)
            storage.initialize()
            pairing = PairingManager(storage)
            code = pairing.create_claim_code()
            pairing.try_claim(
                IncomingMessage(
                    "claim", "chat", "owner", f"/claim {code}", is_direct=True
                )
            )

            transport = make_pymax_transport(settings)
            responder = make_llm_responder(settings, storage)

        self.assertEqual(transport.options.timezone, "Asia/Yekaterinburg")
        self.assertEqual(
            responder.provider.config.timezone_offset_minutes,
            300,
        )
        self.assertEqual(responder.provider.config.provider_name, "openai")
        self.assertEqual(responder.provider.config.model, "gpt-5.6-luna")
