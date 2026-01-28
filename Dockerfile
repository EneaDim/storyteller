FROM python:3.12-slim

WORKDIR /srv

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    ffmpeg \
    sox \
    zstd \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://ollama.com/install.sh | sh

COPY requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt

COPY app /srv/app
COPY bot /srv/bot
COPY scripts /srv/scripts

RUN mkdir -p /srv/logs /srv/tmp /srv/.ollama

ENV PYTHONUNBUFFERED=1
ENV OLLAMA_MODELS=/srv/.ollama

CMD ["/srv/scripts/prod_api.sh"]
