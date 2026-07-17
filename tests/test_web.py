import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import AioHTTPTestCase

from app.config import Settings
from app.storage import Storage
from app.web import PROVIDER_INFO_KEY, create_web_app


class WebDashboardTests(AioHTTPTestCase):
    async def get_application(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp.name)
        with patch.dict(
            os.environ,
            {
                "APP_DATA_DIR": str(root / "data"),
                "LLM_API_KEY_FILE": str(root / "secrets" / "openrouter-api-key.txt"),
                "WEB_AUTOSTART_AI": "false",
            },
            clear=True,
        ):
            self.settings = Settings.from_env()
        self.storage = Storage(self.settings.database_path)
        return create_web_app(self.settings, self.storage)

    async def asyncTearDown(self) -> None:
        await super().asyncTearDown()
        self.temp.cleanup()

    @staticmethod
    def mutation_headers() -> dict[str, str]:
        return {"X-Requested-With": "max-ai-dashboard"}

    async def test_index_status_and_security_headers(self) -> None:
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("MAX AI Control", await response.text())

        response = await self.client.get("/api/status")
        payload = await response.json()
        self.assertFalse(payload["running"])
        self.assertTrue(payload["security"]["loopback_only"])

    async def test_mutations_require_dashboard_marker(self) -> None:
        response = await self.client.post("/api/conversations", json={})
        self.assertEqual(response.status, 403)

    async def test_conversation_key_settings_stats_and_backup(self) -> None:
        response = await self.client.post(
            "/api/conversations", json={}, headers=self.mutation_headers()
        )
        self.assertEqual(response.status, 201)
        conversation = await response.json()

        response = await self.client.get(
            f"/api/conversations/{conversation['id']}/messages"
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["messages"], [])

        key = "sk-or-v1-" + "z" * 40
        response = await self.client.put(
            "/api/keys/openrouter",
            json={"key": key},
            headers=self.mutation_headers(),
        )
        self.assertEqual(response.status, 200)
        self.assertNotIn(key, await response.text())

        response = await self.client.put(
            "/api/settings",
            json={"llm_model": "openrouter/free"},
            headers=self.mutation_headers(),
        )
        self.assertEqual(response.status, 200)

        response = await self.client.get("/api/stats")
        self.assertEqual(response.status, 200)
        self.assertIn("summary", await response.json())

        response = await self.client.get("/api/backup")
        self.assertEqual(response.status, 200)
        body = await response.read()
        self.assertNotIn(key.encode(), body)

    async def test_provider_and_n8n_endpoints(self) -> None:
        self.app[PROVIDER_INFO_KEY].models = AsyncMock(
            return_value=[{"id": "gpt-5.6-luna", "name": "GPT-5.6 Luna"}]
        )
        response = await self.client.get("/api/provider/models")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["items"][0]["id"], "gpt-5.6-luna")

        response = await self.client.put(
            "/api/n8n",
            json={
                "enabled": False,
                "url": "https://n8n.example/webhook/test-id",
            },
            headers=self.mutation_headers(),
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertFalse(payload["enabled"])
        self.assertNotIn("test-id", payload["url_masked"])
