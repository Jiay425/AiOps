# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANGGRAPH_STRICT_MSGPACK=true \
    OPS_HOST=0.0.0.0 \
    OPS_PORT=8099

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-21-jdk-headless maven \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY src ./src
COPY docs ./docs
COPY fixtures ./fixtures
COPY samples ./samples
RUN --mount=type=cache,target=/root/.cache/pip pip install .

EXPOSE 8099
CMD ["python", "-m", "ops_autoagent.main"]
