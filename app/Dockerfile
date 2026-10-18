# syntax=docker/dockerfile:1.7
# SynapseGrid — optimised for Google Cloud Run (listens on $PORT, default 8080)

FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --upgrade pip && /opt/venv/bin/pip install -r requirements.txt

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8080
RUN useradd --create-home --uid 10001 synapse
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=synapse:synapse main.py agents.py grid.py optimizer.py verifier.py gemma.py evaluation.py ./
COPY --chown=synapse:synapse static ./static
USER synapse
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/health', timeout=2)" || exit 1
# Single worker on purpose: grid state, ledger and Pub/Sub bus live in-process.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*' --timeout-keep-alive 75"]
