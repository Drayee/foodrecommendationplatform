# syntax=docker/dockerfile:1
# ===========================================================
# 寻味 · 美食推荐平台
# 说明：镜像只装运行依赖；PostgreSQL 由 docker-compose 提供，
#      Ollama 与高德 API 属外部服务，通过 .env 指向宿主机/公网。
# ===========================================================
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# psycopg 的运行时依赖 + 健康检查工具
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-prod.txt ./
RUN pip install -r requirements.txt -r requirements-prod.txt

COPY . .

# 运行期产物不应打进镜像（.dockerignore 已排除，这里再兜一层）
RUN mkdir -p logs data/index staticfiles

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health/ || exit 1

CMD ["gunicorn", "RAGDjange.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--threads", "4", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
