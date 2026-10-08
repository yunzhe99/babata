FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/var/lib/babata-tokyo/home \
    XDG_CACHE_HOME=/tmp/babata-cache

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ARG CODEX_VERSION=0.160.0
COPY scripts/install_codex.py /tmp/install_codex.py
RUN python /tmp/install_codex.py ${CODEX_VERSION} && rm /tmp/install_codex.py
RUN useradd --create-home --uid 10001 babata \
    && mkdir -p /var/lib/babata-tokyo/home /var/lib/babata-tokyo/workspace /photos \
    && chown -R 10001:10001 /var/lib/babata-tokyo /photos
COPY babata ./babata
COPY LICENSE ./LICENSE
COPY NOTICE ./NOTICE
USER 10001:10001

EXPOSE 8000 8001
CMD ["uvicorn", "babata.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
