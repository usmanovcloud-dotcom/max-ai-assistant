import tempfile
import unittest
from pathlib import Path

from app.pairing import ClaimResult, OwnerGate, PairingManager
from app.storage import Storage
from app.transport import IncomingMessage


class PairingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.storage = Storage(Path(self.temp.name) / "test.sqlite3")
        self.storage.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def message(self, text: str, sender: str = "owner", chat: str = "private") -> IncomingMessage:
        return IncomingMessage("m1", chat, sender, text, is_direct=True)

    def test_claim_is_one_time_and_binds_sender_and_chat(self) -> None:
        pairing = PairingManager(self.storage, ttl_seconds=300)
        code = pairing.create_claim_code(now=100)

        self.assertEqual(pairing.try_claim(self.message(f"/claim {code}"), now=101), ClaimResult.CLAIMED)
        owner = self.storage.get_owner()
        self.assertIsNotNone(owner)
        self.assertEqual((owner.max_user_id, owner.chat_id), ("owner", "private"))

        second = self.message(f"/claim {code}", sender="attacker", chat="other")
        self.assertEqual(pairing.try_claim(second, now=102), ClaimResult.ALREADY_CLAIMED)

    def test_invalid_and_expired_codes_do_not_claim(self) -> None:
        pairing = PairingManager(self.storage, ttl_seconds=60)
        pairing.create_claim_code(now=100)
        self.assertEqual(pairing.try_claim(self.message("/claim wrong"), now=101), ClaimResult.INVALID)
        self.assertEqual(pairing.try_claim(self.message("/claim wrong"), now=161), ClaimResult.EXPIRED)
        self.assertIsNone(self.storage.get_owner())

    def test_owner_gate_requires_exact_sender_chat_and_incoming_direction(self) -> None:
        pairing = PairingManager(self.storage)
        code = pairing.create_claim_code(now=100)
        pairing.try_claim(self.message(f"/claim {code}"), now=101)
        gate = OwnerGate(self.storage)

        self.assertTrue(gate.authorize(self.message("hello")))
        self.assertFalse(gate.authorize(self.message("hello", sender="other")))
        self.assertFalse(gate.authorize(self.message("hello", chat="group")))
        self.assertFalse(gate.authorize(IncomingMessage("m2", "private", "owner", "hello", True, True)))
        self.assertFalse(gate.authorize(IncomingMessage("m3", "private", None, "hello", is_direct=True)))

    def test_claim_from_group_chat_is_rejected(self) -> None:
        pairing = PairingManager(self.storage)
        code = pairing.create_claim_code(now=100)
        group_message = IncomingMessage("m1", "group", "owner", f"/claim {code}")
        self.assertEqual(pairing.try_claim(group_message, now=101), ClaimResult.INVALID)
        self.assertIsNone(self.storage.get_owner())
