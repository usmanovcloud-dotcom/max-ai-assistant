FROM python:3.12.11-slim-bookworm

LABEL org.opencontainers.image.source="https://github.com/usmanovcloud-dotcom/max-ai-assistant" \
      org.opencontainers.image.title="MAX AI Assistant" \
      org.opencontainers.image.description="Private MAX assistant with an OpenAI-powered local dashboard"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_DATA_DIR=/data \
    LLM_PROVIDER=openai \
    LLM_BASE_URL=https://api.openai.com/v1 \
    LLM_MODEL=gpt-5.6-luna \
    LLM_API_KEY_FILE=/run/secrets/max-ai/openai-api-key.txt \
    MAX_2FA_FILE=/run/secrets/max-ai/max-2fa.txt \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8765 \
    CONTAINER_MODE=true

WORKDIR /app

COPY pyproject.toml requirements.lock README.md ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock

COPY app ./app

RUN groupadd --gid 10001 assistant \
    && useradd --uid 10001 --gid assistant --no-create-home --shell /usr/sbin/nologin assistant \
    && mkdir -p /data /run/secrets/max-ai \
    && chown -R assistant:assistant /data /run/secrets/max-ai

USER 10001:10001

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=8s --start-period=20s --retries=3 \
    CMD ["python", "-m", "app.main", "web-healthcheck"]

CMD ["python", "-m", "app.main", "web"]
