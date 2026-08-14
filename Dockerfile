FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANGGRAPH_STRICT_MSGPACK=true \
    OPS_HOST=0.0.0.0 \
    OPS_PORT=8099

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY docs ./docs
COPY fixtures ./fixtures
RUN pip install --no-cache-dir .

EXPOSE 8099
CMD ["python", "-m", "ops_autoagent.main"]
