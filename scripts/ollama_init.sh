#!/bin/sh
# ollama_init.sh
# - aspetta che il server Ollama risponda
# - fa pull del modello scelto

set -e
set -x

MODEL="${OLLAMA_MODEL:-qwen2.5:3b-instruct}"
HOST="${OLLAMA_HOST:-http://ollama:11434}"

echo "[ollama_init] Using OLLAMA_HOST=${HOST}"
echo "[ollama_init] Target model: ${MODEL}"

# IMPORTANTISSIMO: forziamo il client a usare HOST
export OLLAMA_HOST="${HOST}"

echo "[ollama_init] Waiting for Ollama server to respond to 'ollama list'..."

n=1
while [ "$n" -le 120 ]; do
  # Facciamo vedere l'errore ogni tanto (debug utile)
  if ollama list >/tmp/ollama_list.out 2>/tmp/ollama_list.err; then
    echo "[ollama_init] Ollama is ready."
    break
  fi

  if [ "$n" = "1" ] || [ "$n" = "10" ] || [ "$n" = "30" ] || [ "$n" = "60" ] || [ "$n" = "120" ]; then
    echo "[ollama_init] Still not ready... (${n}/120)"
    echo "[ollama_init] last error:"
    tail -n 5 /tmp/ollama_list.err || true
  fi

  n=$((n + 1))
  sleep 1
done

if [ "$n" -gt 120 ]; then
  echo "[ollama_init] ERROR: Timeout waiting for Ollama at ${HOST}"
  exit 1
fi

echo "[ollama_init] Pulling model: ${MODEL}"
k=1
while [ "$k" -le 5 ]; do
  if ollama pull "${MODEL}"; then
    echo "[ollama_init] Model pulled successfully."
    echo "[ollama_init] Done."
    exit 0
  fi
  echo "[ollama_init] Pull failed, retry (${k}/5)..."
  k=$((k + 1))
  sleep 2
done

echo "[ollama_init] ERROR: Failed to pull model after retries."
exit 1

