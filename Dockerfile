FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && useradd --uid 10001 --no-create-home app
USER app

# Cloud Run (and most hosts) set $PORT.
CMD ["google-photos-mcp", "remote"]
