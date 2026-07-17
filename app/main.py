from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os

from app.config import Settings
from app.storage import Storage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MAX AI Assistant stage-1 core")
    parser.add_argument(
        "command",
        choices=(
            "init-db",
            "healthcheck",
            "provide-2fa",
            "provide-llm-key",
            "provide-openrouter-key",
            "provide-openai-key",
            "gate0",
            "ai",
            "web",
            "web-healthcheck",
        ),
        help="Local maintenance or the MAX echo Gate 0 runtime",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings.ensure_runtime_directories()
    if args.command == "provide-2fa":
        password = getpass.getpass("Введите пароль 2FA локально: ").strip()
        if not password:
            raise SystemExit("Пароль 2FA не может быть пустым")
        temporary = settings.two_factor_secret_path.with_suffix(".tmp")
        temporary.write_text(password, encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, settings.two_factor_secret_path)
        logging.getLogger(__name__).info("Локальный пароль 2FA подготовлен")
        return 0
    if args.command in {"provide-llm-key", "provide-openrouter-key", "provide-openai-key"}:
        requested_provider = {
            "provide-openrouter-key": "openrouter",
            "provide-openai-key": "openai",
        }.get(args.command, settings.llm_provider)
        provider_label = {
            "openrouter": "OpenRouter",
            "openai": "OpenAI",
        }.get(requested_provider, "LLM")
        api_key = getpass.getpass(f"Введите {provider_label} API key локально: ").strip()
        if not api_key:
            raise SystemExit(f"{provider_label} API key не может быть пустым")
        if requested_provider == "openrouter" and not api_key.startswith("sk-or-v1-"):
            raise SystemExit("Формат OpenRouter API key не распознан")
        if requested_provider != settings.llm_provider:
            raise SystemExit(
                f"Сейчас выбран LLM_PROVIDER={settings.llm_provider}; "
                f"используйте команду для этого провайдера"
            )
        temporary = settings.llm_api_key_file.with_suffix(".tmp")
        temporary.write_text(api_key, encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, settings.llm_api_key_file)
        logging.getLogger(__name__).info("Локальный %s API key сохранён", provider_label)
        return 0
    storage = Storage(settings.database_path)
    storage.initialize()
    if args.command == "init-db":
        logging.getLogger(__name__).info("Local database initialized")
        return 0
    if args.command == "healthcheck":
        return 0
    if args.command == "web-healthcheck":
        import json
        import urllib.request

        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{settings.web_port}/api/status", timeout=5
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return 0 if response.status == 200 and "running" in payload else 1
        except (OSError, ValueError):
            return 1
    if args.command == "web":
        from app.web import run_web

        run_web(settings)
        return 0

    from app.dashboard import effective_settings

    settings = effective_settings(settings, storage)

    from app.pairing import PairingManager
    from app.runtime import (
        make_llm_responder,
        make_pymax_transport,
        run_ai,
        run_gate0,
    )

    pairing = PairingManager(storage, settings.claim_ttl_seconds)
    if storage.get_owner() is None:
        code = pairing.create_claim_code()
        settings.claim_command_path.write_text(f"/claim {code}\n", encoding="utf-8")
        try:
            settings.claim_command_path.chmod(0o600)
        except OSError:
            pass
        logging.getLogger(__name__).warning(
            "Owner is not paired; local claim command written to %s",
            settings.claim_command_path,
        )

    transport = make_pymax_transport(settings)
    try:
        if args.command == "ai":
            if not settings.llm_api_key_file.is_file():
                raise SystemExit(
                    "LLM API key file отсутствует. Сначала выполните provide-llm-key."
                )
            responder = make_llm_responder(settings, storage)
            asyncio.run(
                run_ai(
                    storage,
                    transport,
                    responder,
                    pairing,
                    settings.claim_command_path,
                    settings.max_message_chars,
                    settings.llm_history_messages,
                )
            )
        else:
            asyncio.run(
                run_gate0(
                    storage,
                    transport,
                    pairing,
                    settings.claim_command_path,
                    settings.max_message_chars,
                )
            )
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Gate 0 stopped by user")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
