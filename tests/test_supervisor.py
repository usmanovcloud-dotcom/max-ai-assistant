import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.monitoring import RingLogHandler
from app.storage import Storage
from app.supervisor import AssistantSupervisor


class SupervisorRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_web_start_enters_pairing_and_exposes_claim_command(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory, patch.dict(
            os.environ, {"APP_DATA_DIR": directory}, clear=True
        ):
            settings = Settings.from_env()
            storage = Storage(settings.database_path)
            storage.initialize()
            supervisor = AssistantSupervisor(settings, storage, RingLogHandler())

            async def waiting_gate0(*args, **kwargs) -> None:
                await asyncio.Event().wait()

            with patch(
                "app.supervisor.make_pymax_transport", return_value=object()
            ), patch("app.supervisor.run_gate0", side_effect=waiting_gate0):
                await supervisor.start()
                await asyncio.sleep(0)

                status = supervisor.status()
                self.assertTrue(status["running"])
                self.assertEqual(status["state"], "pairing")
                self.assertFalse(status["owner_paired"])
                self.assertTrue(
                    status["max"]["claim_command"].startswith("/claim ")
                )

                await supervisor.stop()

    async def test_stop_does_not_hang_when_runtime_cleanup_stalls(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory, patch.dict(
            os.environ, {"APP_DATA_DIR": directory}, clear=True
        ):
            settings = Settings.from_env()
            storage = Storage(settings.database_path)
            storage.initialize()
            supervisor = AssistantSupervisor(settings, storage, RingLogHandler())

            release_cleanup = asyncio.Event()

            async def stalled_runtime() -> None:
                try:
                    await asyncio.Event().wait()
                finally:
                    await release_cleanup.wait()

            task = asyncio.create_task(stalled_runtime())
            await asyncio.sleep(0)
            supervisor._task = task
            supervisor._transport = object()
            supervisor._state = "running"
            supervisor.STOP_TIMEOUT_SECONDS = 0.01

            await supervisor.stop()

            self.assertIsNone(supervisor._task)
            self.assertEqual(supervisor.status()["state"], "stopped")
            self.assertFalse(supervisor.status()["max"]["connected"])

            release_cleanup.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
