# Образ для запуска бота в контейнере (Podman / Docker).
# Базовый образ с uv и Python 3.12.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Сначала только манифесты — слой с зависимостями кэшируется и не пересобирается при правке кода.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Код бота.
COPY *.py ./

# ВНИМАНИЕ: вшиваем .env (с секретами) прямо в образ — образ становится самодостаточным.
# Это сделано осознанно для ЛИЧНОГО локального использования.
# НИКОГДА не публикуй этот образ в публичном реестре и не делись им — внутри лежат твои ключи.
# (.env должен лежать рядом с Dockerfile во время сборки; в git он не коммитится.)
COPY .env ./

# python-dotenv в config.py подхватит /app/.env автоматически.
CMD [".venv/bin/python", "main.py"]
