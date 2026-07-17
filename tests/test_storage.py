import tempfile
import unittest
from pathlib import Path

from app.storage import DailyLimitExceeded, ReservationState, Storage


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.storage = Storage(Path(self.temp.name) / "test.sqlite3")
        self.storage.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_message_lifecycle_and_cached_response(self) -> None:
        first = self.storage.reserve_message("m1", "c1", now=1)
        self.assertEqual(first.state, ReservationState.NEW)
        self.assertEqual(self.storage.reserve_message("m1", "c1", now=2).state, ReservationState.PROCESSING)

        self.storage.store_response("m1", "answer", now=3)
        ready = self.storage.reserve_message("m1", "c1", now=4)
        self.assertEqual(ready.state, ReservationState.RESPONSE_READY)
        self.assertEqual(ready.response_text, "answer")

        self.storage.mark_sent("m1", now=5)
        self.assertEqual(self.storage.reserve_message("m1", "c1", now=6).state, ReservationState.SENT)

    def test_failed_generation_can_retry(self) -> None:
        self.storage.reserve_message("m1", "c1", now=1)
        self.storage.mark_failed("m1", "TimeoutError", now=2)
        self.assertEqual(self.storage.reserve_message("m1", "c1", now=3).state, ReservationState.RETRY)

    def test_conversation_generations_hide_old_history(self) -> None:
        self.storage.append_message("c1", "user", "old", now=1)
        self.assertEqual(self.storage.get_history("c1"), [("user", "old")])
        self.assertEqual(self.storage.new_conversation("c1"), 2)
        self.storage.append_message("c1", "user", "new", now=2)
        self.assertEqual(self.storage.get_history("c1"), [("user", "new")])

    def test_model_settings_round_trip(self) -> None:
        self.assertIsNone(self.storage.get_model_setting("model"))
        self.storage.set_model_setting("model", "fake-model", now=1)
        self.assertEqual(self.storage.get_model_setting("model"), "fake-model")

    def test_daily_usage_is_reserved_atomically_and_limited(self) -> None:
        usage = self.storage.reserve_daily_request("2026-07-15", 2, 100)
        self.assertEqual(usage.requests, 1)
        self.storage.record_token_usage("2026-07-15", 30, 20)
        usage = self.storage.reserve_daily_request("2026-07-15", 2, 100)
        self.assertEqual(usage.requests, 2)
        self.assertEqual((usage.input_tokens, usage.output_tokens), (30, 20))
        with self.assertRaises(DailyLimitExceeded):
            self.storage.reserve_daily_request("2026-07-15", 2, 100)

    def test_web_conversation_lifecycle(self) -> None:
        conversation = self.storage.create_web_conversation(now=10)
        self.storage.append_message(conversation.id, "user", "hello", now=11)
        self.storage.append_message(conversation.id, "assistant", "hi", now=12)
        self.storage.touch_web_conversation(conversation.id, title="Greeting", now=13)

        listed = self.storage.list_web_conversations()
        self.assertEqual(listed[0].title, "Greeting")
        self.assertEqual(
            [(item.role, item.content) for item in self.storage.get_messages(conversation.id)],
            [("user", "hello"), ("assistant", "hi")],
        )
        self.assertTrue(self.storage.delete_web_conversation(conversation.id))
        self.assertIsNone(self.storage.get_web_conversation(conversation.id))

    def test_llm_events_stats_and_audit_do_not_require_message_text(self) -> None:
        self.storage.record_llm_event(
            provider="openrouter",
            model="free",
            source="web",
            status="success",
            latency_ms=250,
            input_tokens=10,
            output_tokens=5,
            cost=0.001,
            now=100,
        )
        self.storage.record_llm_event(
            provider="openrouter",
            model="free",
            source="max",
            status="error",
            latency_ms=50,
            error_code="Timeout",
            now=101,
        )
        stats = self.storage.get_llm_stats(0)
        self.assertEqual(stats["summary"]["total"], 2)
        self.assertEqual(stats["summary"]["success"], 1)
        self.assertEqual(stats["summary"]["errors"], 1)
        self.storage.add_audit("settings_updated", "fields=model", now=102)
        self.assertEqual(self.storage.get_audit(1)[0]["action"], "settings_updated")
