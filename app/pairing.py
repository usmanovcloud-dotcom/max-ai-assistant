from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from enum import Enum

from app.storage import Owner, Storage
from app.transport import IncomingMessage


class ClaimResult(str, Enum):
    NOT_A_CLAIM = "not_a_claim"
    CLAIMED = "claimed"
    INVALID = "invalid"
    EXPIRED = "expired"
    NOT_CONFIGURED = "not_configured"
    ALREADY_CLAIMED = "already_claimed"


def _hash_claim(code: str) -> bytes:
    return hashlib.sha256(code.encode("utf-8")).digest()


@dataclass(slots=True)
class PairingManager:
    storage: Storage
    ttl_seconds: int = 900

    def create_claim_code(self, now: int | None = None) -> str:
        timestamp = int(time.time()) if now is None else now
        code = secrets.token_urlsafe(32)
        self.storage.set_pairing_code(
            _hash_claim(code), timestamp + self.ttl_seconds, now=timestamp
        )
        return code

    def try_claim(self, message: IncomingMessage, now: int | None = None) -> ClaimResult:
        prefix = "/claim "
        if not message.text.startswith(prefix):
            return ClaimResult.NOT_A_CLAIM
        code = message.text[len(prefix):].strip()
        if (
            not code
            or not message.sender_id
            or not message.chat_id
            or message.is_outgoing
            or not message.is_direct
        ):
            return ClaimResult.INVALID
        result = self.storage.claim_owner(
            _hash_claim(code), message.sender_id, message.chat_id, now=now
        )
        return ClaimResult(result)


@dataclass(slots=True)
class OwnerGate:
    storage: Storage

    def authorize(self, message: IncomingMessage) -> bool:
        if message.is_outgoing or not message.is_direct or not message.sender_id:
            return False
        owner: Owner | None = self.storage.get_owner()
        return bool(
            owner
            and secrets.compare_digest(owner.max_user_id, message.sender_id)
            and secrets.compare_digest(owner.chat_id, message.chat_id)
        )
