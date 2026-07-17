from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

from app.config import Settings
from app.dashboard import SecretStore, effective_settings
from app.storage import Storage


class ProviderInfoService:
    def __init__(self, base: Settings, storage: Storage, secrets: SecretStore) -> None:
        self.base = base
        self.storage = storage
        self.secrets = secrets

    async def models(self) -> list[dict[str, Any]]:
        settings = effective_settings(self.base, self.storage)
        key_path = self.secrets.path(settings.llm_provider)
        if not key_path.is_file():
            raise ValueError("API key is not configured")
        payload = await self._get_json(
            settings.llm_base_url.rstrip("/") + "/models",
            key_path.read_text(encoding="utf-8").strip(),
        )
        items = payload.get("data") or []
        result: list[dict[str, Any]] = []
        for item in items:
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            if settings.llm_provider == "openai" and not self._openai_text_model(model_id):
                continue
            pricing = item.get("pricing") or {}
            result.append(
                {
                    "id": model_id,
                    "name": str(item.get("name") or model_id),
                    "owned_by": item.get("owned_by"),
                    "context_length": item.get("context_length"),
                    "prompt_price": pricing.get("prompt"),
                    "completion_price": pricing.get("completion"),
                }
            )
        if settings.llm_provider == "openrouter" and not any(
            item["id"] == "openrouter/free" for item in result
        ):
            result.insert(
                0,
                {
                    "id": "openrouter/free",
                    "name": "OpenRouter Free Models Router",
                    "owned_by": "openrouter",
                    "context_length": None,
                    "prompt_price": "0",
                    "completion_price": "0",
                },
            )
        result.sort(
            key=lambda item: (
                item["id"] != settings.llm_model,
                not str(item["id"]).endswith(":free"),
                str(item["id"]),
            )
        )
        return result[:500]

    async def account(self, provider: str | None = None) -> dict[str, Any]:
        settings = effective_settings(self.base, self.storage)
        selected_provider = provider or settings.llm_provider
        if selected_provider == "openai":
            return await self._openai_account(settings)
        return await self._openrouter_account(settings)

    async def _openai_account(self, settings: Settings) -> dict[str, Any]:
        admin_path = self.secrets.path("openai-admin")
        if not admin_path.is_file():
            return {
                "available": False,
                "provider": "openai",
                "reason": "OpenAI Admin key is not configured",
                "budget": settings.openai_monthly_budget_usd,
            }
        now = datetime.now(timezone.utc)
        start = int(datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp())
        url = (
            "https://api.openai.com/v1/organization/costs"
            f"?start_time={start}&bucket_width=1d&limit=31"
        )
        payload = await self._get_json(
            url, admin_path.read_text(encoding="utf-8").strip()
        )
        spent = 0.0
        currency = "usd"
        for bucket in payload.get("data") or []:
            for result in bucket.get("results") or []:
                amount = result.get("amount") or {}
                try:
                    spent += float(amount.get("value") or 0)
                except (TypeError, ValueError):
                    continue
                currency = str(amount.get("currency") or currency)
        budget = settings.openai_monthly_budget_usd
        remaining = max(0.0, budget - spent)
        return {
            "available": True,
            "provider": "openai",
            "basis": "monthly_budget",
            "budget": budget,
            "used": spent,
            "remaining": remaining,
            "remaining_percent": round(remaining / budget * 100, 2),
            "currency": currency,
            "updated_at": int(time.time()),
        }

    async def _openrouter_account(self, settings: Settings) -> dict[str, Any]:
        key_path = self.secrets.path("openrouter")
        if not key_path.is_file():
            return {
                "available": False,
                "provider": "openrouter",
                "reason": "OpenRouter key is not configured",
            }
        key = key_path.read_text(encoding="utf-8").strip()
        try:
            payload = await self._get_json("https://openrouter.ai/api/v1/credits", key)
            data = payload.get("data") or {}
            total = float(data.get("total_credits") or 0)
            used = float(data.get("total_usage") or 0)
            if total > 0:
                remaining = max(0.0, total - used)
                return {
                    "available": True,
                    "provider": "openrouter",
                    "basis": "credits",
                    "budget": total,
                    "used": used,
                    "remaining": remaining,
                    "remaining_percent": round(remaining / total * 100, 2),
                    "currency": "usd",
                    "updated_at": int(time.time()),
                }
        except RuntimeError:
            pass
        payload = await self._get_json("https://openrouter.ai/api/v1/key", key)
        data = payload.get("data") or {}
        limit = data.get("limit")
        remaining = data.get("limit_remaining")
        if limit is None or remaining is None or float(limit) <= 0:
            return {
                "available": False,
                "provider": "openrouter",
                "reason": "The key has no monetary limit",
                "is_free_tier": bool(data.get("is_free_tier")),
            }
        return {
            "available": True,
            "provider": "openrouter",
            "basis": "key_limit",
            "budget": float(limit),
            "used": float(data.get("usage") or 0),
            "remaining": float(remaining),
            "remaining_percent": round(float(remaining) / float(limit) * 100, 2),
            "currency": "usd",
            "updated_at": int(time.time()),
        }

    @staticmethod
    def _openai_text_model(model_id: str) -> bool:
        lowered = model_id.lower()
        excluded = (
            "audio",
            "realtime",
            "transcribe",
            "tts",
            "image",
            "embedding",
            "moderation",
            "search-preview",
        )
        return (
            lowered.startswith(("gpt-", "o3", "o4"))
            and not any(part in lowered for part in excluded)
        )

    @staticmethod
    async def _get_json(url: str, key: str) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                ) as response:
                    payload = await response.json(content_type=None)
                    if not 200 <= response.status < 300:
                        raise RuntimeError(f"Provider metadata request failed ({response.status})")
                    if not isinstance(payload, dict):
                        raise RuntimeError("Provider metadata response is invalid")
                    return payload
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise RuntimeError("Provider metadata connection failed") from exc
