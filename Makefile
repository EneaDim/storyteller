.DEFAULT_GOAL := help
SHELL := /bin/bash

PROFILE ?= dev

help: ## Mostra help
	@echo ""
	@echo "Usage: make <target> [PROFILE=dev|prod]"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'
	@echo ""

env: ## Crea .env da .env.example se non esiste
	@if [ ! -f .env ]; then cp .env.example .env; echo "Created .env from .env.example"; else echo ".env already exists"; fi

build: ## Build immagini docker
	docker compose --profile $(PROFILE) build

up: ## Avvia stack
	docker compose --profile $(PROFILE) up -d

down: ## Ferma stack (non rimuove volumi)
	docker compose --profile $(PROFILE) down

ps: ## Lista servizi
	docker compose --profile $(PROFILE) ps

restart: ## Restart stack
	$(MAKE) down PROFILE=$(PROFILE)
	$(MAKE) up PROFILE=$(PROFILE)

rebuild:
	docker compose --profile dev down --remove-orphans
	docker compose --profile dev up -d --build

clean: ## Stop + rimuove volumi (DB/MinIO/app_data) ATTENZIONE: distruttivo
	docker compose --profile $(PROFILE) down -v --remove-orphans

# ---- Volume app_data management ----
volume-ls: ## Mostra volumi docker (filtra per progetto)
	@docker volume ls | grep -E 'podcast-telegram|app_data|pg_data|minio_data' || true

volume-inspect: ## Inspect volume app_data
	@docker volume inspect podcast-telegram_app_data 2>/dev/null || docker volume inspect app_data 2>/dev/null || true

volume-prune-app: ## Rimuove SOLO volume app_data (ATTENZIONE: distruttivo)
	@docker volume rm -f podcast-telegram_app_data 2>/dev/null || docker volume rm -f app_data 2>/dev/null || true

urls: ## Mostra URL utili
	@echo "API:      http://localhost:$${API_PORT:-8080}"
	@echo "Demo:     http://localhost:$${API_PORT:-8080}/demo"
	@echo "MinIO UI: http://localhost:$${MINIO_CONSOLE_PORT:-9001}"

# =========================
# Config for checks
# =========================
PROFILE ?= dev

API_URL ?= http://localhost:8080
PIPER_URL ?= http://localhost:8000
OLLAMA_URL ?= http://localhost:11434
MINIO_URL ?= http://localhost:9000
MINIO_CONSOLE_URL ?= http://localhost:9001

# Se ti serve un token bot telegram nel make, passalo al volo:
# make tg-getwebhook TELEGRAM_BOT_TOKEN=xxxxx

# =========================
# Logs
# =========================
logs: ## Follow logs (tutti i servizi)
	docker compose --profile $(PROFILE) logs -f --tail=200

logs-api: ## Follow logs solo API
	docker compose --profile $(PROFILE) logs -f --tail=200 api

logs-worker: ## Follow logs solo worker
	docker compose --profile $(PROFILE) logs -f --tail=200 worker

logs-ollama: ## Follow logs solo Ollama
	docker compose --profile $(PROFILE) logs -f --tail=200 ollama

logs-piper: ## Follow logs solo Piper
	docker compose --profile $(PROFILE) logs -f --tail=200 piper piper_init

logs-minio: ## Follow logs solo MinIO
	docker compose --profile $(PROFILE) logs -f --tail=200 minio minio_init

# =========================
# Core HTTP checks (API/Gradio)
# =========================
check-api-health: ## GET /healthz
	@curl -fsS $(API_URL)/healthz && echo

check-api-demo: ## GET /demo (Gradio UI endpoint)
	@curl -fsS -I $(API_URL)/demo | head -n 15

# (Se hai un endpoint di generazione API; altrimenti usa la demo.)
# check-api-generate:
# 	@curl -fsS -X POST $(API_URL)/api/generate -H "Content-Type: application/json" \
# 	  -d '{"text":"Podcast breve su AI","user_id":"test"}' && echo

# =========================
# Ollama checks
# =========================
check-ollama-tags: ## Ollama: /api/tags
	@curl -fsS $(OLLAMA_URL)/api/tags | head -c 400 && echo

check-ollama-chat: ## Ollama: /api/chat (richiede OLLAMA_MODEL=... in env)
	@bash -lc 'test -n "$$OLLAMA_MODEL" || (echo "Set OLLAMA_MODEL in env (es: qwen2.5:3b-instruct)" && exit 1)'
	@curl -fsS $(OLLAMA_URL)/api/chat -H "Content-Type: application/json" \
	  -d "{\"model\":\"$$OLLAMA_MODEL\",\"stream\":false,\"messages\":[{\"role\":\"user\",\"content\":\"Scrivi una frase in italiano.\"}]}" \
	  | head -c 600 && echo

# Se vuoi testare dal container (rete interna): utile quando host->ollama non esposto
check-ollama-tags-internal: ## Ollama tags da dentro container API (usa http://ollama:11434)
	@docker compose --profile $(PROFILE) exec -T api sh -lc 'curl -fsS http://ollama:11434/api/tags | head -c 400 && echo'

# =========================
# Piper checks (2 voci)
# =========================
check-piper-health: ## Piper: GET /docs (FastAPI) - verifica che server sia su
	@curl -fsS -I $(PIPER_URL)/docs | head -n 10

check-piper-tts-female: ## Piper TTS female -> test_f.mp3
	@curl -fsS -X POST $(PIPER_URL)/tts -H "Content-Type: application/json" \
	  -d '{"text":"Ciao, sono la narratrice. Questa è una prova audio.","voice":"female","format":"mp3"}' \
	  --output test_f.mp3
	@ls -lh test_f.mp3

check-piper-tts-male: ## Piper TTS male -> test_m.mp3
	@curl -fsS -X POST $(PIPER_URL)/tts -H "Content-Type: application/json" \
	  -d '{"text":"Ciao, sono l esperto. Questa è una prova audio.","voice":"male","format":"mp3"}' \
	  --output test_m.mp3
	@ls -lh test_m.mp3

# =========================
# MinIO checks
# =========================
check-minio-live: ## MinIO: health live endpoint
	@curl -fsS $(MINIO_URL)/minio/health/live && echo

check-minio-ready: ## MinIO: health ready endpoint
	@curl -fsS $(MINIO_URL)/minio/health/ready && echo

# Controlla bucket via mc dentro minio_init (richiede MINIO_ROOT_USER/PASSWORD e S3_BUCKET in .env)
check-minio-bucket: ## MinIO: lista bucket e contenuti (via mc)
	@docker compose --profile $(PROFILE) exec -T minio_init sh -lc '\
	  mc alias set local http://minio:9000 "$${MINIO_ROOT_USER:-minio}" "$${MINIO_ROOT_PASSWORD:-minio123456}" >/dev/null 2>&1 && \
	  echo "Buckets:" && mc ls local && \
	  echo "Objects in $${S3_BUCKET:-podcasts}:" && mc ls local/$${S3_BUCKET:-podcasts} || true \
	'

# =========================
# Redis / Postgres checks
# =========================
check-redis: ## Redis PING (richiede redis-cli nel container redis; di solito c’è)
	@docker compose --profile $(PROFILE) exec -T redis sh -lc 'redis-cli ping'

check-postgres: ## Postgres readiness + versione
	@docker compose --profile $(PROFILE) exec -T postgres sh -lc 'pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB && psql -U $$POSTGRES_USER -d $$POSTGRES_DB -c "select version();"'

# =========================
# Telegram checks (opzionali)
# =========================
tg-getwebhook: ## Telegram: getWebhookInfo (passa TELEGRAM_BOT_TOKEN=...)
	@bash -lc 'test -n "$$TELEGRAM_BOT_TOKEN" || (echo "Usage: make tg-getwebhook TELEGRAM_BOT_TOKEN=xxxx" && exit 1)'
	@curl -fsS "https://api.telegram.org/bot$$TELEGRAM_BOT_TOKEN/getWebhookInfo" && echo

tg-setwebhook: ## Telegram: setWebhook (passa TELEGRAM_BOT_TOKEN, PUBLIC_BASE_URL, TELEGRAM_WEBHOOK_SECRET)
	@bash -lc 'test -n "$$TELEGRAM_BOT_TOKEN" || (echo "Missing TELEGRAM_BOT_TOKEN" && exit 1)'
	@bash -lc 'test -n "$$PUBLIC_BASE_URL" || (echo "Missing PUBLIC_BASE_URL (es: https://xxxx.ngrok-free.app)" && exit 1)'
	@bash -lc 'test -n "$$TELEGRAM_WEBHOOK_SECRET" || (echo "Missing TELEGRAM_WEBHOOK_SECRET" && exit 1)'
	@curl -fsS -X POST "https://api.telegram.org/bot$$TELEGRAM_BOT_TOKEN/setWebhook" \
	  -d "url=$$PUBLIC_BASE_URL/webhooks/telegram/$$TELEGRAM_WEBHOOK_SECRET" && echo

# =========================
# Meta: check suite
# =========================
check: ## Suite rapida: api + ollama + piper + minio
	@$(MAKE) check-api-health
	@$(MAKE) check-ollama-tags
	@$(MAKE) check-piper-health
	@$(MAKE) check-minio-live

