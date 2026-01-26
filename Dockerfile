FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# System deps: ffmpeg for opus, sox optional (useful if you ever need it)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg sox libsox-fmt-all libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install -U pip && pip install -r /app/requirements.txt

COPY . /app

# Railway sets PORT
CMD ["bash", "scripts/prod_api.sh"]
