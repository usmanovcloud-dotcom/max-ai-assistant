from __future__ import annotations

import os
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Mapping

import aiohttp

from app.config import Settings
from app.providers.openai_compatible import safe_provider_error_details
from app.storage import Storage


EDITABLE_FIELDS: dict[str, type] = {
    "llm_provider": str,
    "llm_base_url": str,
    "llm_model": str,
    "llm_timeout": float,
    "llm_daily_limit": int,
    "llm_daily_token_limit": int,
    "llm_max_output_tokens": int,
    "llm_max_retries": int,
    "llm_history_messages": int,
    "llm_max_input_chars": int,
    "llm_instructions": str,
    "openai_monthly_budget_usd": float,
}


def key_path_for_provider(base: Settings, provider: str) -> Path:
    return base.llm_api_key_file.parent / f"{provider}-api-key.txt"


def effective_settings(base: Settings, storage: Storage) -> Settings:
    saved = storage.get_model_settings()
    updates: dict[str, Any] = {}
    for field, converter in EDITABLE_FIELDS.items():
        raw = saved.get(field)
        if raw is None:
            continue
        updates[field] = converter(raw)
    provider = str(updates.get("llm_provider", base.llm_provider)).strip().lower()
    updates["llm_provider"] = provider
    updates["llm_api_key_file"] = key_path_for_provider(base, provider)
    candidate = replace(base, **updates)
    candidate.validate()
    return candidate


def save_settings(base: Settings, storage: Storage, values: Mapping[str, Any]) -> Settings:
    unknown = set(values) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
    current = effective_settings(base, storage)
    updates: dict[str, Any] = {}
    for field, converter in EDITABLE_FIELDS.items():
        if field not in values:
            continue
        value = values[field]
        if converter is str:
            converted = str(value).strip()
        else:
            converted = converter(value)
        updates[field] = converted
    provider = str(updates.get("llm_provider", current.llm_provider)).lower()
    updates["llm_provider"] = provider
    updates["llm_api_key_file"] = key_path_for_provider(base, provider)
    candidate = replace(current, **updates)
    candidate.validate()
    for field in EDITABLE_FIELDS:
        if field in updates:
            storage.set_model_setting(field, str(updates[field]))
    storage.add_audit("settings_updated", f"fields={','.join(sorted(values))}")
    return candidate


def public_settings(settings: Settings) -> dict[str, Any]:
    data = asdict(settings)
    allowed = set(EDITABLE_FIELDS)
    return {key: value for key, value in data.items() if key in allowed}


class SecretStore:
    def __init__(self, base: Settings, storage: Storage) -> None:
        self.base = base
        self.storage = storage

    def path(self, provider: str) -> Path:
        if provider not in {"openrouter", "openai", "openai-admin"}:
            raise ValueError("Unsupported provider")
        return key_path_for_provider(self.base, provider)

    def status(self, provider: str) -> dict[str, Any]:
        path = self.path(provider)
        if not path.is_file():
            return {"configured": False, "masked": None}
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            return {"configured": False, "masked": None}
        start = value[: min(8, max(3, len(value) - 4))]
        return {"configured": True, "masked": f"{start}…{value[-4:]}"}

    def save(self, provider: str, value: str) -> None:
        key = value.strip()
        if len(key) < 20:
            raise ValueError("API key is too short")
        if provider == "openrouter" and not key.startswith("sk-or-v1-"):
            raise ValueError("OpenRouter key format is not recognized")
        if provider in {"openai", "openai-admin"} and not key.startswith("sk-"):
            raise ValueError("OpenAI key format is not recognized")
        path = self.path(provider)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(key, encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        self.storage.add_audit("api_key_saved", f"provider={provider}")

    def delete(self, provider: str) -> bool:
        path = self.path(provider)
        existed = path.exists()
        path.unlink(missing_ok=True)
        if existed:
            self.storage.add_audit("api_key_deleted", f"provider={provider}")
        return existed

    async def test(self, provider: str) -> dict[str, Any]:
        path = self.path(provider)
        if not path.is_file():
            raise ValueError("API key is not configured")
        key = path.read_text(encoding="utf-8").strip()
        base_url = {
            "openrouter": "https://openrouter.ai/api/v1",
            "openai": "https://api.openai.com/v1",
        }[provider]
        timeout = aiohttp.ClientTimeout(total=15)
        headers = {"Authorization": f"Bearer {key}"}
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{base_url}/models", headers=headers) as response:
                    ok = 200 <= response.status < 300
                    payload: Mapping[str, Any] = {}
                    if not ok:
                        try:
                            loaded = await response.json(content_type=None)
                            if isinstance(loaded, Mapping):
                                payload = loaded
                        except (aiohttp.ClientError, ValueError):
                            payload = {}
                    self.storage.add_audit(
                        "api_key_tested", f"provider={provider},ok={str(ok).lower()}"
                    )
                    result: dict[str, Any] = {"ok": ok, "status": response.status}
                    if not ok:
                        result["error"] = safe_provider_error_details(
                            response.status, payload
                        )
                    return result
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise RuntimeError("Provider connection failed") from exc
