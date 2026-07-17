from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from dataclasses import asdict
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from app.config import Settings
from app import __version__
from app.dashboard import SecretStore, effective_settings, public_settings, save_settings
from app.monitoring import RingLogHandler
from app.storage import Storage
from app.supervisor import AssistantSupervisor
from app.provider_info import ProviderInfoService


Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
STATIC_DIR = Path(__file__).with_name("web_static")
SETTINGS_KEY = web.AppKey("settings", Settings)
STORAGE_KEY = web.AppKey("storage", Storage)
LOGS_KEY = web.AppKey("logs", RingLogHandler)
SUPERVISOR_KEY = web.AppKey("supervisor", AssistantSupervisor)
SECRETS_KEY = web.AppKey("secrets", SecretStore)
PROVIDER_INFO_KEY = web.AppKey("provider_info", ProviderInfoService)


@web.middleware
async def error_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except KeyError as exc:
        return web.json_response({"error": str(exc).strip("'")}, status=404)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:
        logging.getLogger("max_ai_assistant.web").error(
            "Dashboard request failed path=%s error=%s", request.path, type(exc).__name__
        )
        return web.json_response({"error": str(exc) or type(exc).__name__}, status=500)


@web.middleware
async def local_security_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    remote = request.remote
    if (
        not request.app[SETTINGS_KEY].container_mode
        and remote not in {None, "127.0.0.1", "::1"}
    ):
        raise web.HTTPForbidden(text="Dashboard is loopback-only")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.headers.get("X-Requested-With") != "max-ai-dashboard":
            raise web.HTTPForbidden(text="Missing dashboard request marker")
        origin = request.headers.get("Origin")
        if origin:
            allowed = {
                f"http://127.0.0.1:{request.app[SETTINGS_KEY].web_port}",
                f"http://localhost:{request.app[SETTINGS_KEY].web_port}",
                f"http://[::1]:{request.app[SETTINGS_KEY].web_port}",
            }
            if request.app[SETTINGS_KEY].container_mode:
                allowed.add(f"{request.scheme}://{request.host}")
            if origin.rstrip("/") not in allowed:
                raise web.HTTPForbidden(text="Cross-origin request denied")
    response = await handler(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    )
    return response


def _services(request: web.Request) -> tuple[Storage, AssistantSupervisor, SecretStore]:
    return request.app[STORAGE_KEY], request.app[SUPERVISOR_KEY], request.app[SECRETS_KEY]


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def status(request: web.Request) -> web.Response:
    storage, supervisor, secrets = _services(request)
    payload = supervisor.status()
    payload["keys"] = {
        provider: secrets.status(provider)
        for provider in ("openai", "openai-admin", "openrouter")
    }
    payload["app_version"] = __version__
    payload["security"] = {
        "loopback_only": not request.app[SETTINGS_KEY].container_mode,
        "public_ports": False,
    }
    payload["audit_latest"] = storage.get_audit(5)
    return web.json_response(payload)


async def runtime_action(request: web.Request) -> web.Response:
    _, supervisor, _ = _services(request)
    action = request.match_info["action"]
    if action == "start":
        await supervisor.start()
    elif action == "stop":
        await supervisor.stop()
    elif action == "restart":
        await supervisor.restart()
    else:
        raise web.HTTPNotFound()
    return web.json_response(supervisor.status())


async def get_settings(request: web.Request) -> web.Response:
    storage, supervisor, _ = _services(request)
    settings = supervisor.settings()
    return web.json_response(
        {"settings": public_settings(settings), "saved": storage.get_model_settings()}
    )


async def put_settings(request: web.Request) -> web.Response:
    storage, supervisor, _ = _services(request)
    values = await request.json()
    if not isinstance(values, dict):
        raise ValueError("Settings payload must be an object")
    was_running = supervisor.running
    candidate = save_settings(request.app[SETTINGS_KEY], storage, values)
    if was_running:
        await supervisor.restart()
    return web.json_response({"settings": public_settings(candidate), "restarted": was_running})


async def get_keys(request: web.Request) -> web.Response:
    _, _, secrets = _services(request)
    return web.json_response(
        {
            provider: secrets.status(provider)
            for provider in ("openai", "openai-admin", "openrouter")
        }
    )


async def put_key(request: web.Request) -> web.Response:
    _, _, secrets = _services(request)
    provider = request.match_info["provider"]
    payload = await request.json()
    secrets.save(provider, str(payload.get("key", "")))
    return web.json_response(secrets.status(provider))


async def delete_key(request: web.Request) -> web.Response:
    _, supervisor, secrets = _services(request)
    provider = request.match_info["provider"]
    if supervisor.running and supervisor.settings().llm_provider == provider:
        await supervisor.stop()
    return web.json_response({"deleted": secrets.delete(provider)})


async def test_key(request: web.Request) -> web.Response:
    _, _, secrets = _services(request)
    provider = request.match_info["provider"]
    if provider == "openai-admin":
        result = await request.app[PROVIDER_INFO_KEY].account("openai")
        return web.json_response({"ok": bool(result.get("available")), "account": result})
    return web.json_response(await secrets.test(provider))


async def provider_models(request: web.Request) -> web.Response:
    return web.json_response(
        {"items": await request.app[PROVIDER_INFO_KEY].models()}
    )


async def provider_account(request: web.Request) -> web.Response:
    storage, supervisor, _ = _services(request)
    result = await request.app[PROVIDER_INFO_KEY].account()
    settings = supervisor.settings()
    local_tz = timezone(timedelta(minutes=settings.llm_timezone_offset_minutes))
    usage = storage.get_daily_usage(datetime.now(local_tz).date().isoformat())
    result["daily_requests"] = {
        "used": usage.requests,
        "limit": settings.llm_daily_limit,
        "remaining": max(0, settings.llm_daily_limit - usage.requests),
        "remaining_percent": round(
            max(0, settings.llm_daily_limit - usage.requests)
            / settings.llm_daily_limit
            * 100,
            2,
        ),
    }
    return web.json_response(result)


async def n8n_status(request: web.Request) -> web.Response:
    _, supervisor, _ = _services(request)
    return web.json_response(supervisor.n8n.status())


async def n8n_configure(request: web.Request) -> web.Response:
    _, supervisor, _ = _services(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise ValueError("n8n configuration must be an object")
    return web.json_response(
        supervisor.n8n.configure(
            enabled=bool(payload.get("enabled")),
            url=str(payload["url"]) if "url" in payload else None,
            token=str(payload["token"]) if "token" in payload else None,
            clear_token=bool(payload.get("clear_token")),
        )
    )


async def n8n_test(request: web.Request) -> web.Response:
    _, supervisor, _ = _services(request)
    return web.json_response(await supervisor.n8n.test())


async def list_conversations(request: web.Request) -> web.Response:
    storage, _, _ = _services(request)
    return web.json_response(
        {"items": [asdict(item) for item in storage.list_web_conversations()]}
    )


async def create_conversation(request: web.Request) -> web.Response:
    storage, _, _ = _services(request)
    payload = await request.json() if request.can_read_body else {}
    title = str(payload.get("title", "Новый диалог")) if isinstance(payload, dict) else "Новый диалог"
    conversation = storage.create_web_conversation(title)
    storage.add_audit("web_conversation_created")
    return web.json_response(asdict(conversation), status=201)


async def conversation_messages(request: web.Request) -> web.Response:
    storage, _, _ = _services(request)
    conversation_id = request.match_info["conversation_id"]
    conversation = storage.get_web_conversation(conversation_id)
    if conversation is None:
        raise KeyError("Conversation not found")
    return web.json_response(
        {
            "conversation": asdict(conversation),
            "messages": [asdict(item) for item in storage.get_messages(conversation_id)],
        }
    )


async def send_chat_message(request: web.Request) -> web.Response:
    _, supervisor, _ = _services(request)
    payload = await request.json()
    answer = await supervisor.chat(
        request.match_info["conversation_id"], str(payload.get("text", ""))
    )
    return web.json_response({"answer": answer})


async def clear_conversation(request: web.Request) -> web.Response:
    storage, _, _ = _services(request)
    conversation_id = request.match_info["conversation_id"]
    if storage.get_web_conversation(conversation_id) is None:
        raise KeyError("Conversation not found")
    generation = storage.new_conversation(conversation_id)
    storage.touch_web_conversation(conversation_id, title="Новый диалог")
    storage.add_audit("web_conversation_cleared")
    return web.json_response({"generation": generation})


async def delete_conversation(request: web.Request) -> web.Response:
    storage, _, _ = _services(request)
    deleted = storage.delete_web_conversation(request.match_info["conversation_id"])
    if not deleted:
        raise KeyError("Conversation not found")
    storage.add_audit("web_conversation_deleted")
    return web.json_response({"deleted": True})


async def export_conversation(request: web.Request) -> web.Response:
    storage, _, _ = _services(request)
    conversation_id = request.match_info["conversation_id"]
    conversation = storage.get_web_conversation(conversation_id)
    if conversation is None:
        raise KeyError("Conversation not found")
    lines = [f"# {conversation.title}", ""]
    labels = {"user": "Пользователь", "assistant": "Ассистент", "system": "Система"}
    for message in storage.get_messages(conversation_id, 1000):
        lines.extend((f"## {labels.get(message.role, message.role)}", "", message.content, ""))
    return web.Response(
        text="\n".join(lines),
        content_type="text/markdown",
        headers={"Content-Disposition": 'attachment; filename="conversation.md"'},
    )


async def send_max_message(request: web.Request) -> web.Response:
    _, supervisor, _ = _services(request)
    payload = await request.json()
    await supervisor.send_to_max(str(payload.get("text", "")))
    return web.json_response({"sent": True})


async def qr_image(request: web.Request) -> web.StreamResponse:
    _, supervisor, _ = _services(request)
    path = supervisor.settings().qr_path
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def refresh_qr(request: web.Request) -> web.Response:
    _, supervisor, _ = _services(request)
    await supervisor.request_new_qr()
    return web.json_response({"requested": True})


async def provide_2fa(request: web.Request) -> web.Response:
    storage, supervisor, _ = _services(request)
    payload = await request.json()
    password = str(payload.get("password", "")).strip()
    if not password:
        raise ValueError("2FA password must not be empty")
    path = supervisor.settings().two_factor_secret_path
    temporary = path.with_suffix(".tmp")
    temporary.write_text(password, encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    os.replace(temporary, path)
    storage.add_audit("max_2fa_provided")
    return web.json_response({"saved": True})


async def revoke_max_session(request: web.Request) -> web.Response:
    _, supervisor, _ = _services(request)
    payload = await request.json()
    if payload.get("confirm") is not True:
        raise ValueError("Explicit confirmation is required")
    await supervisor.revoke_max_session()
    return web.json_response({"revoked": True})


async def stats(request: web.Request) -> web.Response:
    storage, supervisor, _ = _services(request)
    try:
        days = max(1, min(90, int(request.query.get("days", "14"))))
    except ValueError:
        days = 14
    since = int(time.time()) - days * 86400
    result = storage.get_llm_stats(since)
    settings = supervisor.settings()
    local_tz = timezone(timedelta(minutes=settings.llm_timezone_offset_minutes))
    today = storage.get_daily_usage(datetime.now(local_tz).date().isoformat())
    result["limits"] = {
        "requests": settings.llm_daily_limit,
        "tokens": settings.llm_daily_token_limit,
        "today": asdict(today),
    }
    return web.json_response(result)


async def logs(request: web.Request) -> web.Response:
    storage, _, _ = _services(request)
    return web.json_response(
        {"runtime": request.app[LOGS_KEY].snapshot(150), "audit": storage.get_audit(150)}
    )


async def backup(request: web.Request) -> web.Response:
    storage, supervisor, _ = _services(request)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        copy_path = Path(directory) / "assistant.sqlite3"
        source = sqlite3.connect(storage.path)
        destination = sqlite3.connect(copy_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(copy_path, "assistant.sqlite3")
            archive.writestr(
                "settings.json",
                json.dumps(public_settings(supervisor.settings()), ensure_ascii=False, indent=2),
            )
            archive.writestr(
                "README.txt",
                "Резервная копия не содержит API-ключи и MAX session.\n",
            )
    storage.add_audit("safe_backup_created")
    return web.Response(
        body=buffer.getvalue(),
        content_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="max-ai-backup.zip"'},
    )


def create_web_app(settings: Settings, storage: Storage | None = None) -> web.Application:
    storage = storage or Storage(settings.database_path)
    storage.initialize()
    logs_handler = RingLogHandler()
    logging.getLogger().addHandler(logs_handler)
    supervisor = AssistantSupervisor(settings, storage, logs_handler)
    application = web.Application(
        middlewares=[error_middleware, local_security_middleware],
        client_max_size=1024 * 1024,
    )
    application[SETTINGS_KEY] = settings
    application[STORAGE_KEY] = storage
    application[LOGS_KEY] = logs_handler
    application[SUPERVISOR_KEY] = supervisor
    secrets = SecretStore(settings, storage)
    application[SECRETS_KEY] = secrets
    application[PROVIDER_INFO_KEY] = ProviderInfoService(settings, storage, secrets)
    application.router.add_get("/", index)
    application.router.add_static("/assets", STATIC_DIR, show_index=False)
    application.router.add_get("/api/status", status)
    application.router.add_post("/api/runtime/{action}", runtime_action)
    application.router.add_get("/api/settings", get_settings)
    application.router.add_put("/api/settings", put_settings)
    application.router.add_get("/api/keys", get_keys)
    application.router.add_put("/api/keys/{provider}", put_key)
    application.router.add_delete("/api/keys/{provider}", delete_key)
    application.router.add_post("/api/keys/{provider}/test", test_key)
    application.router.add_get("/api/provider/models", provider_models)
    application.router.add_get("/api/provider/account", provider_account)
    application.router.add_get("/api/n8n", n8n_status)
    application.router.add_put("/api/n8n", n8n_configure)
    application.router.add_post("/api/n8n/test", n8n_test)
    application.router.add_get("/api/conversations", list_conversations)
    application.router.add_post("/api/conversations", create_conversation)
    application.router.add_get(
        "/api/conversations/{conversation_id}/messages", conversation_messages
    )
    application.router.add_post(
        "/api/conversations/{conversation_id}/messages", send_chat_message
    )
    application.router.add_post(
        "/api/conversations/{conversation_id}/clear", clear_conversation
    )
    application.router.add_delete(
        "/api/conversations/{conversation_id}", delete_conversation
    )
    application.router.add_get(
        "/api/conversations/{conversation_id}/export", export_conversation
    )
    application.router.add_post("/api/max/send", send_max_message)
    application.router.add_get("/api/max/qr", qr_image)
    application.router.add_post("/api/max/qr/refresh", refresh_qr)
    application.router.add_post("/api/max/2fa", provide_2fa)
    application.router.add_post("/api/max/revoke", revoke_max_session)
    application.router.add_get("/api/stats", stats)
    application.router.add_get("/api/logs", logs)
    application.router.add_get("/api/backup", backup)

    async def startup(app: web.Application) -> None:
        if settings.web_autostart_ai:
            try:
                await app[SUPERVISOR_KEY].start()
            except Exception as exc:
                logging.getLogger("max_ai_assistant.web").warning(
                    "Assistant autostart skipped error=%s", type(exc).__name__
                )

    async def cleanup(app: web.Application) -> None:
        await app[SUPERVISOR_KEY].stop()
        logging.getLogger().removeHandler(app[LOGS_KEY])

    application.on_startup.append(startup)
    application.on_cleanup.append(cleanup)
    return application


def run_web(settings: Settings) -> None:
    web.run_app(
        create_web_app(settings),
        host=settings.web_host,
        port=settings.web_port,
        print=lambda message: logging.getLogger("max_ai_assistant.web").info(message),
    )
