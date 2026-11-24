FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Agent menjalankan perintah verifikasi di container bersarang, sehingga image
# ini membutuhkan Docker CLI. Socket daemon dipasang oleh klien di host.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

ENV WORKSPACE_DIR=/workspace \
    AGENT_LOGS_DIR=/logs

ENTRYPOINT ["coding-agent"]
CMD ["--help"]
