# 🎙️ AI Podcast Generator

A fully dockerized, production-ready **Generative AI Podcast Stack** featuring:

* 🧠 Local or API-based LLM (Ollama, HuggingFace, OpenAI, Google GenAI)
* 🎤 Local Speech-to-Text (Faster-Whisper) or API STT
* 🔊 Dual-Voice Text-to-Speech (Piper – Narrator + Expert)
* 🗄️ PostgreSQL + Redis + MinIO (S3-compatible storage)
* 🤖 Telegram Bot Integration
* 🎛️ Gradio Demo Interface (with embedded audio player)
* 🐳 Fully containerized with Docker
* ⚙️ Dev & Production profiles

---

# 🚀 What This Project Does

This stack generates a **two-voice podcast episode** from either:

* ✍️ Text input
* 🎙️ Voice message (OGG/OPUS)

Pipeline:

1. Audio → STT (local Faster-Whisper)
2. Transcript → LLM → Dialog Script (NARRATOR + EXPERT)
3. Script → TTS (Piper, female narrator + male expert)
4. Audio segments → Concatenation → Final MP3
5. (Optional) Store outputs in MinIO (S3-compatible)
6. Play audio directly in Gradio

Everything runs locally if desired. No paid APIs required.

---

# 🏗️ Architecture

## Core Services

| Service       | Purpose                        |
| ------------- | ------------------------------ |
| `api`         | FastAPI + Gradio app           |
| `worker`      | Background job processing (RQ) |
| `ollama`      | Local LLM server               |
| `ollama_init` | Auto-pulls LLM model           |
| `piper`       | Local TTS server (2 voices)    |
| `piper_init`  | Downloads Piper voice models   |
| `postgres`    | Metadata storage               |
| `redis`       | Queue backend                  |
| `minio`       | S3-compatible object storage   |
| `minio_init`  | Auto-creates bucket            |

---

# 📁 Project Structure

```
.
├── app.py
├── docker-compose.yml
├── Makefile
├── requirements.txt
├── .env
├── scripts/
│   ├── ollama_init.sh
│   ├── piper_init.sh
│   └── piper_server.py
└── README.md
```

---

# ⚙️ Configuration (.env)

## LLM

```env
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen2.5:3b-instruct
```

Other supported providers:

* `openai`
* `huggingface`
* `google_genai`

---

## STT (Free Local)

```env
STT_PROVIDER=faster_whisper_local
STT_MODEL=base
STT_LANGUAGE=it
```

---

## TTS (Free Local)

```env
TTS_PROVIDER=piper_http
PIPER_TTS_URL=http://piper:8000
PIPER_VOICE_F=it_IT-paola-medium
PIPER_VOICE_M=it_IT-riccardo-x_low
PODCAST_VOICE_NARRATOR=female
PODCAST_VOICE_EXPERT=male
```

---

## Storage

```env
S3_ENDPOINT=http://minio:9000
S3_BUCKET=podcasts
STORE_OUTPUTS=0
```

---

# 🧪 Development Mode

Start everything:

```bash
docker compose --profile dev up -d --build
```

Open:

```
http://localhost:8080/demo
```

You can:

* Record voice
* Paste text
* Generate a full podcast
* Listen directly in the browser

---

# 🧰 Useful Make Commands

### Follow logs

```bash
make logs PROFILE=dev
```

### Health checks

```bash
make check
```

### Test Piper voices

```bash
make check-piper-tts-female
make check-piper-tts-male
```

### Test Ollama

```bash
make check-ollama-tags
make check-ollama-chat OLLAMA_MODEL=qwen2.5:3b-instruct
```

---

# 🤖 Telegram Integration

Supports button-based Telegram interaction.

Set:

```env
TELEGRAM_BOT_TOKEN=...
PUBLIC_BASE_URL=https://your-ngrok-url
TELEGRAM_WEBHOOK_SECRET=...
```

Then:

```bash
make tg-setwebhook TELEGRAM_BOT_TOKEN=xxx PUBLIC_BASE_URL=https://... TELEGRAM_WEBHOOK_SECRET=xxx
```

---

# 🧠 Switching AI Providers

You can switch providers by editing `.env` only.

## From Ollama → HuggingFace

```env
LLM_PROVIDER=huggingface
HUGGINGFACE_TOKEN=...
HUGGINGFACE_INFERENCE_URL=...
```

## From Local STT → OpenAI Whisper

```env
STT_PROVIDER=openai_whisper
OPENAI_API_KEY=...
```

Restart services after change.

---

# 🎛️ Dev vs Production

## Dev

* No subscription required
* STORE_OUTPUTS=0
* Local models only

## Production

* STORE_OUTPUTS=1
* Stripe integration
* Public object URLs
* Telegram payment gating

---

# 💡 Design Philosophy

* Minimal files
* Maximum modularity
* Provider-agnostic AI stack
* Fully containerized
* Local-first, cloud-optional
* Explicit configuration via environment variables

---

# 🛠️ Troubleshooting

## Audio stops early

Ensure MP3 concatenation uses re-encoding (not stream copy).

## STT returns stub message

Check:

```env
STT_PROVIDER=faster_whisper_local
```

Rebuild containers.

## Piper 500 error

Ensure voice models were downloaded via `piper_init`.

---

# 🔥 Future Extensions

* Music intro/outro layer
* Voice cloning
* Multi-language podcasts
* RAG-based podcast episodes
* Scheduled daily podcast bot
* SaaS deployment template

---

# 🧾 License

MIT — Use freely, modify boldly.

---

# ✨ Final Thoughts

This project is more than a demo.

It is a **complete generative AI micro-platform**:

* Swap models
* Change providers
* Monetize via Telegram
* Deploy locally or in the cloud

Build fast. Iterate faster. Ship intelligently.

🎙️ Happy Podcasting.

