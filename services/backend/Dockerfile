FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && groupadd --system foodlog \
    && useradd --system --gid foodlog --home-dir /nonexistent foodlog

USER foodlog

EXPOSE 8080

CMD ["uvicorn", "foodlog_backend.main:app", "--host", "0.0.0.0", "--port", "8080"]
