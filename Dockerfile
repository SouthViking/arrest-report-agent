ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system arrestagent \
    && useradd --system --gid arrestagent --home-dir /app arrestagent

COPY requirements.txt ./

RUN pip install --requirement requirements.txt

COPY src ./src
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations

USER arrestagent

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; port = os.getenv('PORT', os.getenv('HEALTH_PORT', '8080')); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=2)"]

CMD ["python", "-m", "arrest_report_agent.main"]
