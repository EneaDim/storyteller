FROM python:3.12-slim

WORKDIR /srv

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Install Ollama (Linux)
RUN curl -fsSL https://ollama.com/install.sh | sh

COPY requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt

COPY app /srv/app
COPY bot /srv/bot
COPY scripts /srv/scripts

RUN mkdir -p /srv/logs /srv/tmp
ENV PYTHONUNBUFFERED=1

# IMPORTANT: model cache directory (mount a Railway Volume here)
ENV OLLAMA_MODELS=/srv/.ollama

# default command: llm+tts service entry
CMD ["/srv/scripts/prod_api.sh"]
