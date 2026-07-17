import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.providers.openai_compatible import (
    OpenAIAuthenticationError,
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
    OpenAIInputTooLong,
    OpenAIPermissionError,
    OpenAIQuotaError,
    OpenAITransientError,
)
from app.storage import DailyLimitExceeded, Storage
from app.transport import IncomingAttachment


def success_payload(text: str = "Привет") -> dict:
    return {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {"input_tokens": 12, "output_tokens": 4},
    }


class OpenAIProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp.name)
        self.key_path = root / "key.txt"
        self.key_path.write_text("test-secret-key", encoding="utf-8")
        self.storage = Storage(root / "db.sqlite3")
        self.storage.initialize()

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def config(self, **changes) -> OpenAICompatibleConfig:
        values = {
            "base_url": "https://api.openai.com/v1",
            "api_key_file": self.key_path,
            "model": "gpt-5.6-luna",
            "max_retries": 0,
            "safety_identifier": "max_hash",
        }
        values.update(changes)
        return OpenAICompatibleConfig(**values)

    async def test_responses_request_is_stateless_and_usage_is_recorded(self) -> None:
        captured = {}

        async def post(url, headers, body, timeout):
            captured.update(url=url, headers=headers, body=body, timeout=timeout)
            return 200, success_payload(), {}

        provider = OpenAICompatibleProvider(self.config(), self.storage, http_post=post)
        result = await provider.complete([("user", "Привет")])

        self.assertEqual(result, "Привет")
        self.assertEqual(captured["url"], "https://api.openai.com/v1/responses")
        self.assertFalse(captured["body"]["store"])
        self.assertEqual(captured["body"]["model"], "gpt-5.6-luna")
        self.assertEqual(captured["body"]["safety_identifier"], "max_hash")
        self.assertNotIn("test-secret-key", str(captured["body"]))
        usage = self.storage.get_daily_usage(provider._today())
        self.assertEqual((usage.requests, usage.input_tokens, usage.output_tokens), (1, 12, 4))

    async def test_openrouter_profile_uses_safe_compatible_request(self) -> None:
        captured = {}

        async def post(url, headers, body, timeout):
            captured.update(url=url, headers=headers, body=body, timeout=timeout)
            return 200, success_payload("ok"), {}

        provider = OpenAICompatibleProvider(
            self.config(
                provider_name="openrouter",
                base_url="https://openrouter.ai/api/v1",
                model="openrouter/free",
            ),
            self.storage,
            http_post=post,
        )

        self.assertEqual(await provider.complete([("user", "Привет")]), "ok")
        self.assertEqual(captured["url"], "https://openrouter.ai/api/v1/responses")
        self.assertEqual(captured["headers"]["X-OpenRouter-Title"], "MAX AI Assistant")
        self.assertEqual(captured["body"]["model"], "openrouter/free")
        self.assertFalse(captured["body"]["store"])
        self.assertNotIn("safety_identifier", captured["body"])
        self.assertNotIn("text", captured["body"])

    async def test_files_and_images_use_responses_multimodal_content(self) -> None:
        captured = {}

        async def post(url, headers, body, timeout):
            captured["body"] = body
            return 200, success_payload("ok"), {}

        provider = OpenAICompatibleProvider(self.config(), self.storage, http_post=post)
        provider._download_attachment = AsyncMock(return_value=b"pdf bytes")
        attachments = (
            IncomingAttachment("file", "report.pdf", "https://max.example/report"),
            IncomingAttachment("image", "photo.jpg", "https://max.example/photo"),
        )
        self.assertEqual(
            await provider.complete(
                [("user", "Что в этих вложениях? [Файл: report.pdf]")],
                attachments=attachments,
            ),
            "ok",
        )
        content = captured["body"]["input"][-1]["content"]
        self.assertEqual(content[0]["type"], "input_text")
        self.assertEqual(
            content[1],
            {
                "type": "input_file",
                "filename": "report.pdf",
                "file_data": "data:application/pdf;base64,cGRmIGJ5dGVz",
            },
        )
        self.assertNotIn("max.example/report", str(captured["body"]))
        provider._download_attachment.assert_awaited_once_with(attachments[0])
        self.assertEqual(
            content[2],
            {"type": "input_image", "image_url": "https://max.example/photo"},
        )

    async def test_audio_is_transcribed_then_added_to_transient_prompt(self) -> None:
        captured = {}

        async def post(url, headers, body, timeout):
            captured["body"] = body
            return 200, success_payload("summary"), {}

        provider = OpenAICompatibleProvider(self.config(), self.storage, http_post=post)
        provider._transcribe_audio = AsyncMock(return_value="текст голосового сообщения")
        audio = IncomingAttachment(
            "audio", "voice-42.ogg", "https://max.example/voice?signature=secret"
        )
        self.assertEqual(
            await provider.complete(
                [("user", "Что сказано?")], attachments=(audio,)
            ),
            "summary",
        )
        prepared = captured["body"]["input"][-1]["content"]
        self.assertIn("текст голосового сообщения", prepared)
        self.assertNotIn("signature=secret", str(captured["body"]))
        provider._transcribe_audio.assert_awaited_once_with(audio)

    async def test_file_download_enforces_actual_size_limit(self) -> None:
        provider = OpenAICompatibleProvider(
            self.config(max_attachment_bytes=4), self.storage, http_post=AsyncMock()
        )
        attachment = IncomingAttachment(
            "file", "report.pdf", "https://max.example/report", size=1
        )

        class Content:
            async def iter_chunked(self, size):
                yield b"123"
                yield b"45"

        class Response:
            status = 200
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def get(self, url):
                return Response()

        with patch("aiohttp.ClientSession", return_value=Session()):
            with self.assertRaises(OpenAIInputTooLong):
                await provider._download_attachment(attachment)

    async def test_payment_required_is_reported_as_quota_error(self) -> None:
        post = AsyncMock(return_value=(402, {"error": {"message": "quota"}}, {}))
        provider = OpenAICompatibleProvider(
            self.config(provider_name="openrouter"), self.storage, http_post=post
        )
        with self.assertRaises(OpenAIQuotaError):
            await provider.complete([("user", "hello")])
        self.assertEqual(post.await_count, 1)

    async def test_authentication_error_is_not_retried(self) -> None:
        post = AsyncMock(return_value=(401, {"error": {"message": "secret"}}, {}))
        provider = OpenAICompatibleProvider(self.config(max_retries=2), self.storage, http_post=post)
        with self.assertRaises(OpenAIAuthenticationError):
            await provider.complete([("user", "hello")])
        self.assertEqual(post.await_count, 1)

    async def test_forbidden_error_preserves_only_safe_diagnostics(self) -> None:
        post = AsyncMock(
            return_value=(
                403,
                {
                    "error": {
                        "code": "permission_denied",
                        "message": "Key sk-proj-secretvalue is blocked",
                        "metadata": {"flagged_input": "private prompt"},
                    },
                    "error_type": "permission_denied",
                },
                {},
            )
        )
        provider = OpenAICompatibleProvider(self.config(), self.storage, http_post=post)
        with self.assertRaises(OpenAIPermissionError) as caught:
            await provider.complete([("user", "hello")])

        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(caught.exception.details["type"], "permission_denied")
        self.assertIn("[redacted]", caught.exception.details["message"])
        self.assertNotIn("private prompt", str(caught.exception.details))
        self.assertEqual(post.await_count, 1)

    async def test_transient_error_is_retried_without_new_daily_reservation(self) -> None:
        post = AsyncMock(
            side_effect=[
                (429, {}, {"Retry-After": "0"}),
                (200, success_payload("ok"), {}),
            ]
        )
        provider = OpenAICompatibleProvider(self.config(max_retries=1), self.storage, http_post=post)
        with patch("app.providers.openai_compatible.asyncio.sleep", new=AsyncMock()):
            self.assertEqual(await provider.complete([("user", "hello")]), "ok")
        self.assertEqual(post.await_count, 2)
        self.assertEqual(self.storage.get_daily_usage(provider._today()).requests, 1)

    async def test_insufficient_quota_is_not_retried(self) -> None:
        post = AsyncMock(
            return_value=(
                429,
                {"error": {"type": "insufficient_quota", "code": "insufficient_quota"}},
                {},
            )
        )
        provider = OpenAICompatibleProvider(
            self.config(max_retries=2), self.storage, http_post=post
        )
        with self.assertRaises(OpenAIQuotaError):
            await provider.complete([("user", "hello")])
        self.assertEqual(post.await_count, 1)

    async def test_limits_block_before_http_request(self) -> None:
        post = AsyncMock(return_value=(200, success_payload(), {}))
        provider = OpenAICompatibleProvider(
            self.config(daily_request_limit=1), self.storage, http_post=post
        )
        await provider.complete([("user", "one")])
        with self.assertRaises(DailyLimitExceeded):
            await provider.complete([("user", "two")])
        self.assertEqual(post.await_count, 1)

    async def test_oversized_latest_message_is_rejected(self) -> None:
        provider = OpenAICompatibleProvider(
            self.config(max_input_chars=5), self.storage, http_post=AsyncMock()
        )
        with self.assertRaises(OpenAIInputTooLong):
            await provider.complete([("user", "123456")])
