"""
Podcast Generator Template (Telegram-only)
=========================================

Questo file fa TUTTO il lato "API / webhooks / UI demo":

- FastAPI server
- Telegram webhook:
  - riceve testo e vocali
  - usa solo bottoni (InlineKeyboard)
  - crea job in coda (Redis/RQ)
- Stripe payment:
  - crea una Checkout Session e restituisce URL
  - il bot manda un bottone URL per pagare
- Gradio demo:
  - montata sotto /demo
- Storage:
  - MinIO (S3 compat) per input/output (script + mp3)
- Volume /data:
  - persistente, usato per caching e file temporanei (download voce telegram)

Obiettivo: template minimale, pochi file, ma stack reale end-to-end.
"""

import os
import io
import json
import time
import uuid
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, Literal, Tuple

import requests
import stripe
import boto3
from botocore.client import Config as BotoConfig

import redis as redis_lib
from rq import Queue

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, Response

import gradio as gr

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("podcast_api")

# -----------------------------
# Settings
# -----------------------------
# ------------------------------------------------------------
# Tipi provider (così gli env sono "self-documenting")
# ------------------------------------------------------------
ProviderLLM = Literal["openai", "google", "ollama", "huggingface"]
ProviderSTT = Literal["openai_whisper", "google", "local_stub"]
ProviderTTS = Literal["openai", "google", "elevenlabs", "coqui_stub", "qwen3_tts_http"]


@dataclass
class Settings:
    # Ambiente
    env: str = os.getenv("APP_ENV", "dev")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080")

    # Volume persistente applicazione
    data_dir: str = os.getenv("DATA_DIR", "/data")

    # Access control: in dev vogliamo generare senza pagare
    require_subscription: bool = os.getenv("REQUIRE_SUBSCRIPTION", "0").strip() == "1"

    # Redis/RQ
    redis_url: str = os.getenv("REDIS_URL", "redis://redis:6379/0")
    rq_queue: str = os.getenv("RQ_QUEUE", "default")

    # S3/MinIO
    s3_endpoint: str = os.getenv("S3_ENDPOINT", "http://minio:9000")
    s3_region: str = os.getenv("S3_REGION", "us-east-1")
    s3_bucket: str = os.getenv("S3_BUCKET", "podcasts")
    s3_access_key: str = os.getenv("S3_ACCESS_KEY", os.getenv("MINIO_ROOT_USER", "minio"))
    s3_secret_key: str = os.getenv("S3_SECRET_KEY", os.getenv("MINIO_ROOT_PASSWORD", "minio123456"))
    s3_public_base: str = os.getenv("S3_PUBLIC_BASE", "").strip()

    # Provider selection
    llm_provider: ProviderLLM = os.getenv("LLM_PROVIDER", "ollama")
    stt_provider: ProviderSTT = os.getenv("STT_PROVIDER", "local_stub")
    tts_provider: ProviderTTS = os.getenv("TTS_PROVIDER", "coqui_stub")

    # OpenAI
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # Google GenAI SDK (Gemini)
    google_api_key: str = os.getenv("GOOGLE_API_KEY", "")
    google_model: str = os.getenv("GOOGLE_MODEL", "gemini-2.0-flash")

    # Ollama (default Qwen)
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5")

    # Hugging Face (endpoint)
    hf_token: str = os.getenv("HUGGINGFACE_TOKEN", "")
    hf_inference_url: str = os.getenv("HUGGINGFACE_INFERENCE_URL", "")
    hf_model: str = os.getenv("HUGGINGFACE_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct")

    # Qwen3-TTS via HTTP endpoint
    qwen_tts_http_url: str = os.getenv("QWEN_TTS_HTTP_URL", "").strip()
    qwen_tts_http_token: str = os.getenv("QWEN_TTS_HTTP_TOKEN", "").strip()
    qwen_tts_voice: str = os.getenv("QWEN_TTS_VOICE", "default").strip()

    # Telegram
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_webhook_secret: str = os.getenv("TELEGRAM_WEBHOOK_SECRET", "change-me")

    # Stripe
    stripe_secret_key: str = os.getenv("STRIPE_SECRET_KEY", "")
    stripe_price_id: str = os.getenv("STRIPE_PRICE_ID", "")
    stripe_success_url: str = os.getenv("STRIPE_SUCCESS_URL", "http://localhost:8080/stripe/success")
    stripe_cancel_url: str = os.getenv("STRIPE_CANCEL_URL", "http://localhost:8080/stripe/cancel")


S = Settings()

# Assicuriamoci che /data esista (nel container c'è il volume montato)
os.makedirs(S.data_dir, exist_ok=True)

# Stripe init: se manca key, la parte payments risponde con errore chiaro
if S.stripe_secret_key:
    stripe.api_key = S.stripe_secret_key

# Redis/RQ queue
redis_conn = redis_lib.Redis.from_url(S.redis_url)
q = Queue(S.rq_queue, connection=redis_conn)

# S3 client (MinIO o AWS S3)
s3 = boto3.client(
    "s3",
    endpoint_url=S.s3_endpoint,
    aws_access_key_id=S.s3_access_key,
    aws_secret_access_key=S.s3_secret_key,
    region_name=S.s3_region,
    config=BotoConfig(signature_version="s3v4"),
)

# -----------------------------
# Helpers: time, urls, s3
# -----------------------------
def now_ms() -> int:
    """Timestamp utile per debug/health."""
    return int(time.time() * 1000)


def public_file_url(key: str) -> str:
    """
    Restituisce un URL pubblico per il file.

    - Se S3_PUBLIC_BASE è settato, assumiamo che bucket sia servito pubblicamente lì.
    - Altrimenti esponiamo un endpoint API /files/{key} che fa proxy.
      (Minimale e utile per demo; in prod preferisci CDN o presigned URL.)
    """
    if S.s3_public_base.strip():
        # Esempio: https://files.example.com/podcasts/outputs/...
        return f"{S.s3_public_base.rstrip('/')}/{S.s3_bucket}/{key}"
    return f"{S.public_base_url.rstrip('/')}/files/{key}"


def s3_put_bytes(key: str, data: bytes, content_type: str) -> None:
    """Upload bytes su bucket S3/MinIO."""
    s3.put_object(Bucket=S.s3_bucket, Key=key, Body=data, ContentType=content_type)


def s3_get_bytes(key: str) -> bytes:
    """Download bytes da bucket S3/MinIO."""
    obj = s3.get_object(Bucket=S.s3_bucket, Key=key)
    return obj["Body"].read()

def user_has_access(chat_id: str) -> bool:
    """
    Regola richiesta:
    - DEV: non serve abbonamento => access sempre True (REQUIRE_SUBSCRIPTION=0)
    - PROD: se REQUIRE_SUBSCRIPTION=1, serve flag su Redis: subscribed:<chat_id> = "1"

    Nota:
    - Questo è un template: la gestione "vera" di subscription in prod si fa con webhook Stripe.
    - Qui abilitiamo l’utente al ritorno su /stripe/success (vedi funzione sotto).
    """
    if not S.require_subscription:
        return True

    key = f"subscribed:{chat_id}"
    try:
        val = redis_conn.get(key)
        return (val or b"").decode("utf-8") == "1"
    except Exception:
        return False

def parse_dialogue_script(script: str) -> list[tuple[str, str]]:
    """
    Converte lo script in una lista di battute:
        [("NARRATOR", "testo..."), ("EXPERT", "testo..."), ...]

    Regole accettate:
    - righe che iniziano con "NARRATOR:" oppure "EXPERT:"
    - righe vuote ignorate
    - se una riga non ha prefisso, la attacchiamo all'ultima battuta (fallback robusto)

    Questo permette al pipeline di essere resiliente se l'LLM "sfora" leggermente.
    """
    lines = (script or "").splitlines()
    out: list[tuple[str, str]] = []

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        upper = line.upper()
        if upper.startswith("NARRATOR:"):
            text = line.split(":", 1)[1].strip()
            if text:
                out.append(("NARRATOR", text))
            continue

        if upper.startswith("EXPERT:"):
            text = line.split(":", 1)[1].strip()
            if text:
                out.append(("EXPERT", text))
            continue

        # Fallback: se l'LLM mette una riga senza prefisso, la trattiamo come continuazione
        if out:
            spk, prev = out[-1]
            out[-1] = (spk, (prev + " " + line).strip())
        else:
            # Se è la prima riga senza prefisso, assumiamo narratrice
            out.append(("NARRATOR", line))

    return out

def concat_mp3_files(mp3_paths: list[str], out_path: str) -> None:
    """
    Concatena più clip MP3 in un MP3 unico *ricodificando* (NO -c copy).

    Perché:
    - MP3 concatenati con stream copy possono avere header/timestamp incoerenti (soprattutto VBR)
      e alcuni player si bloccano dopo pochi secondi anche se la durata sembra corretta.
    - Ricodificare una volta sola produce un MP3 "standard" e stabile ovunque.

    Requisito: ffmpeg installato nel container.
    """
    import os
    import subprocess
    import tempfile

    if not mp3_paths:
        raise RuntimeError("concat_mp3_files: no input files")

    # file list per concat demuxer
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as f:
        list_path = f.name
        for p in mp3_paths:
            f.write(f"file '{p}'\n")

    try:
        # Ricodifica finale in MP3: robusto
        cmd = [
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_path,
            "-vn",
            "-codec:a", "libmp3lame",
            "-q:a", "4",
            out_path,
        ]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode != 0:
            err = p.stderr.decode("utf-8", errors="ignore")
            raise RuntimeError(f"ffmpeg concat+reencode failed: {err[:1600]}")

        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("ffmpeg produced empty output")

    finally:
        try:
            os.remove(list_path)
        except Exception:
            pass

# -----------------------------
# Telegram API helpers
# -----------------------------
def tg_api(method: str, payload: Optional[dict] = None, files: Optional[dict] = None) -> Dict[str, Any]:
    """
    Wrapper minimale per Telegram Bot API.
    - method: es. 'sendMessage', 'getFile'
    - payload: json/form fields
    - files: multipart se necessario
    """
    if not S.telegram_bot_token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

    url = f"https://api.telegram.org/bot{S.telegram_bot_token}/{method}"
    # Telegram accetta form-data anche senza files; qui usiamo requests in modo semplice
    if files:
        r = requests.post(url, data=payload or {}, files=files, timeout=60)
    else:
        r = requests.post(url, json=payload or {}, timeout=60)

    if r.status_code >= 400:
        raise RuntimeError(f"Telegram API error {r.status_code}: {r.text}")

    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API returned not ok: {data}")
    return data


def tg_send_message(chat_id: str, text: str, buttons: Optional[list] = None) -> None:
    """
    Invia messaggio Telegram con InlineKeyboard (solo bottoni).
    buttons: lista di righe, ogni riga è lista di bottoni.
    Esempio:
      buttons = [
        [{"text":"Paga","url":"..."}],
        [{"text":"New","callback_data":"new"}],
      ]
    """
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    tg_api("sendMessage", payload=payload)


def tg_send_audio(chat_id: str, audio_url: str, caption: str = "") -> None:
    """
    Invia un audio via URL.
    Telegram accetta un URL pubblico come "audio".
    """
    payload = {"chat_id": chat_id, "audio": audio_url, "caption": caption}
    tg_api("sendAudio", payload=payload)


def tg_get_file_bytes(file_id: str) -> Tuple[bytes, str]:
    """
    Scarica un file Telegram dato un file_id.
    Ritorna: (bytes, file_path) dove file_path è il path su Telegram (utile per estensione).
    """
    # 1) getFile -> file_path
    meta = tg_api("getFile", payload={"file_id": file_id})
    file_path = meta["result"]["file_path"]

    # 2) download da file endpoint
    dl_url = f"https://api.telegram.org/file/bot{S.telegram_bot_token}/{file_path}"
    r = requests.get(dl_url, timeout=120)
    r.raise_for_status()
    return r.content, file_path


# -----------------------------
# Providers: LLM / STT / TTS
# -----------------------------
def llm_generate_script(input_text: str) -> str:
    """
    Genera uno script podcast in italiano usando il provider scelto.

    Fix importante:
    - Per Ollama usiamo /api/chat (supporto stabile per modelli instruct).
      Alcune build/container non espongono /api/generate e rispondono 404.

    Implementati:
    - ollama (HTTP /api/chat)
    - openai (REST /v1/responses)
    - google (google-genai SDK)
    - huggingface (endpoint HTTP)
    """
    prompt = (
        "Sei un autore di podcast.\n"
        "Scrivi uno script moderno e coinvolgente in italiano.\n"
        "Vincoli:\n"
        "Formato OBBLIGATORIO:"
        '- Ogni riga deve iniziare con "NARRATOR:" oppure "EXPERT:"'
        "- Nessuna riga senza prefisso"
        "- Frasi brevi e naturali, stile podcast"
        "- Non usare markdown, non usare titoli"
        "- durata parlata 1-2 minuti\n"
        "- apertura con hook, 3 sezioni, chiusura con call-to-action\n"
        "- output solo testo, niente markdown\n\n"
        f"INPUT:\n{input_text.strip()}\n"
    )

    # -------------------------
    # OLLAMA (via /api/chat)
    # -------------------------
    if S.llm_provider == "ollama":
        # Endpoint chat (moderno e consigliato)
        url = f"{S.ollama_base_url.rstrip('/')}/api/chat"
        payload = {
            "model": S.ollama_model,
            "stream": False,
            "messages": [
                {"role": "system", "content": "Rispondi sempre in italiano. Output solo testo."},
                {"role": "user", "content": prompt},
            ],
        }

        r = requests.post(url, json=payload, timeout=180)

        # Se per qualche motivo /api/chat non esistesse, qui vedresti 404:
        # puoi eventualmente fare fallback, ma per ora preferiamo errori espliciti.
        r.raise_for_status()
        data = r.json()

        # Formato tipico:
        # { "message": { "role": "assistant", "content": "..." }, ... }
        msg = (data.get("message") or {}).get("content") or ""
        return str(msg).strip()

    # -------------------------
    # OPENAI (REST)
    # -------------------------
    if S.llm_provider == "openai":
        if not S.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY missing")

        url = "https://api.openai.com/v1/responses"
        headers = {"Authorization": f"Bearer {S.openai_api_key}", "Content-Type": "application/json"}
        payload = {"model": S.openai_model, "input": prompt}

        r = requests.post(url, headers=headers, json=payload, timeout=180)
        r.raise_for_status()
        data = r.json()

        out_text = ""
        for item in data.get("output", []):
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    out_text += c.get("text", "")
        return out_text.strip()

    # -------------------------
    # GOOGLE GENAI (SDK)
    # -------------------------
    if S.llm_provider == "google":
        from google import genai

        api_key = S.google_api_key or os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError("Missing GOOGLE_API_KEY (or GEMINI_API_KEY) for Google GenAI")

        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(model=S.google_model, contents=prompt)

        text = getattr(resp, "text", None)
        if text:
            return str(text).strip()
        return str(resp).strip()

    # -------------------------
    # HUGGINGFACE ENDPOINT
    # -------------------------
    if S.llm_provider == "huggingface":
        if not (S.hf_token and S.hf_inference_url):
            raise RuntimeError("Set HUGGINGFACE_TOKEN and HUGGINGFACE_INFERENCE_URL")

        headers = {"Authorization": f"Bearer {S.hf_token}", "Content-Type": "application/json"}
        payload = {"inputs": prompt, "parameters": {"max_new_tokens": 900}}
        r = requests.post(S.hf_inference_url, headers=headers, json=payload, timeout=180)
        r.raise_for_status()
        data = r.json()

        if isinstance(data, list) and data and isinstance(data[0], dict):
            return str(data[0].get("generated_text", "")).strip()
        return str(data.get("generated_text", "")).strip()

    raise RuntimeError(f"Unknown LLM_PROVIDER={S.llm_provider}")

def stt_transcribe(audio_bytes: bytes, mime: str = "audio/ogg") -> str:
    """
    Speech-to-Text (STT).
    - Modalità free+locale: faster-whisper (CPU) -> STT_PROVIDER=faster_whisper_local
    - Altri provider (es. openai_whisper) puoi lasciarli per prod, ma in dev usiamo locale.

    Note importanti:
    - Telegram voice è spesso OGG/OPUS: lo convertiamo a WAV 16kHz mono con ffmpeg prima di trascrivere.
    - Carichiamo il modello una sola volta (cache globale) per non ricaricarlo a ogni richiesta.
    """
    import os
    import tempfile
    import subprocess

    provider = (getattr(S, "stt_provider", "") or os.getenv("STT_PROVIDER", "stub")).strip().lower()

    # --- Stub (vecchio comportamento) ---
    if provider in ("stub", "none", ""):
        return "Trascrizione (stub): abilita STT_PROVIDER=faster_whisper_local per trascrivere davvero."

    # --- Free + locale: faster-whisper ---
    if provider == "faster_whisper_local":
        # Import qui per non pesare se STT disattivato
        from faster_whisper import WhisperModel

        model_name = (os.getenv("STT_MODEL", "base") or "base").strip()
        language = (os.getenv("STT_LANGUAGE", "it") or "it").strip()

        # Cache globale del modello (caricamento una sola volta)
        global _FW_MODEL
        try:
            _FW_MODEL  # type: ignore[name-defined]
        except NameError:
            _FW_MODEL = None  # type: ignore[name-defined]

        if _FW_MODEL is None:  # type: ignore[name-defined]
            # CPU-friendly (compute_type=int8)
            _FW_MODEL = WhisperModel(model_name, device="cpu", compute_type="int8")  # type: ignore[name-defined]

        # 1) Salva input su file temporaneo
        with tempfile.TemporaryDirectory() as td:
            in_path = os.path.join(td, "in.bin")
            wav_path = os.path.join(td, "audio.wav")

            with open(in_path, "wb") as f:
                f.write(audio_bytes)

            # 2) Converti a WAV 16kHz mono (robusto per OGG/OPUS)
            # ffmpeg deve essere presente nel container (tu già lo installi)
            cmd = [
                "ffmpeg",
                "-y",
                "-i", in_path,
                "-ac", "1",
                "-ar", "16000",
                "-f", "wav",
                wav_path,
            ]
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if p.returncode != 0:
                err = p.stderr.decode("utf-8", errors="ignore")
                raise RuntimeError(f"ffmpeg convert failed: {err[:1200]}")

            # 3) Trascrivi
            segments, info = _FW_MODEL.transcribe(  # type: ignore[name-defined]
                wav_path,
                language=language,
                vad_filter=True,   # aiuta su pause/silenzi
            )

            text_parts = []
            for seg in segments:
                t = (seg.text or "").strip()
                if t:
                    text_parts.append(t)

            return " ".join(text_parts).strip()

    # --- OpenAI Whisper (a pagamento) ---
    if provider == "openai_whisper":
        raise RuntimeError("openai_whisper richiede OPENAI_API_KEY (a pagamento). In dev usa faster_whisper_local.")

    raise RuntimeError(f"Unknown STT_PROVIDER={provider}")

def tts_synthesize(text: str, voice: str = "female") -> bytes:
    """
    Text-to-Speech (TTS) -> ritorna bytes audio (MP3).

    Supporta due voci logiche:
      - voice="female"  -> narratrice
      - voice="male"    -> esperto

    Provider supportati (minimale ma completo):
      - coqui_stub: nessun audio (dev super-light)
      - piper_http: TTS gratuito locale via servizio Docker (piper_server.py)
      - openai: (non usato in modalità free) a pagamento

    Env richieste per piper_http:
      TTS_PROVIDER=piper_http
      PIPER_TTS_URL=http://piper:8000
      PIPER_TTS_FORMAT=mp3
    """
    import os

    # Normalizzazione input
    text = (text or "").strip()
    if not text:
        return b""

    # Normalizzazione voce
    voice_norm = (voice or "female").strip().lower()
    if voice_norm not in ("female", "male"):
        # Fallback sicuro: narratrice
        voice_norm = "female"

    # Provider scelto (dal tuo settings/env)
    provider = getattr(S, "tts_provider", "") or os.getenv("TTS_PROVIDER", "coqui_stub")
    provider = provider.strip().lower()

    # ----------------------------
    # 1) Stub (nessun audio)
    # ----------------------------
    if provider == "coqui_stub":
        return b""

    # ----------------------------
    # 2) Piper via HTTP (consigliato: gratis, locale)
    # ----------------------------
    if provider == "piper_http":
        base = (os.getenv("PIPER_TTS_URL", "") or "").strip()
        fmt = (os.getenv("PIPER_TTS_FORMAT", "mp3") or "mp3").strip().lower()

        if not base:
            raise RuntimeError("TTS_PROVIDER=piper_http ma PIPER_TTS_URL è vuoto")

        # Il nostro piper_server accetta voice "male"/"female" e format "mp3"/"wav"
        payload = {"text": text, "voice": voice_norm, "format": fmt}

        # Timeout lungo: TTS può essere lento su CPU
        r = requests.post(f"{base.rstrip('/')}/tts", json=payload, timeout=240)

        # Se il server risponde con errore, includiamo dettaglio per debug
        if r.status_code != 200:
            raise RuntimeError(f"Piper TTS error {r.status_code}: {r.text[:600]}")

        # In caso di risposta JSON (errori), fermiamo qui
        ct = (r.headers.get("content-type") or "").lower()
        if "application/json" in ct:
            raise RuntimeError(f"Piper TTS returned JSON: {r.text[:600]}")

        return r.content

    # ----------------------------
    # 3) OpenAI (a pagamento) - opzionale
    # ----------------------------
    if provider == "openai":
        raise RuntimeError("OpenAI TTS è a pagamento. In modalità free/local usa piper_http.")


    raise RuntimeError(f"Unknown TTS_PROVIDER={S.tts_provider}")


# -----------------------------
# Job: pipeline podcast
# -----------------------------
def run_podcast_pipeline(user_id: str, input_text: str, input_audio_s3_key: str = "") -> Dict[str, Any]:
    """
    Pipeline unica:
    1) Se c'è audio S3 -> STT -> transcript
       altrimenti transcript = input_text
    2) LLM -> script podcast
    3) TTS -> mp3 (2 voci se dialogato)
    4) (opzionale) Upload su S3/MinIO
    5) Ritorna transcript + script + mp3_bytes (per Gradio)
    """
    import os
    import uuid
    import tempfile

    # 1) Transcript
    transcript = (input_text or "").strip()

    if input_audio_s3_key:
        audio_bytes = s3_get_bytes(input_audio_s3_key)
        transcript = stt_transcribe(audio_bytes, mime="audio/ogg").strip()

        # Se STT fallisce, NON vogliamo generare podcast “a caso”
        if len(transcript) < 8:
            raise RuntimeError("STT vuoto o troppo corto: riprova con un vocale più chiaro o più lungo.")

    if not transcript:
        raise RuntimeError("Empty transcript: provide text or audio")

    # 2) Script LLM (basato SOLO sul transcript reale)
    script = llm_generate_script(transcript)

    # 3) TTS (2 voci se dialogo)
    narrator_voice = os.getenv("PODCAST_VOICE_NARRATOR", "female").strip().lower()
    expert_voice = os.getenv("PODCAST_VOICE_EXPERT", "male").strip().lower()

    audio_mp3: bytes = b""
    if S.tts_provider != "coqui_stub":
        turns = parse_dialogue_script(script)
        is_dialogue = (len(turns) >= 2) and any(spk == "EXPERT" for spk, _ in turns)

        if is_dialogue:
            with tempfile.TemporaryDirectory() as td:
                clip_paths: list[str] = []
                for idx, (speaker, text) in enumerate(turns):
                    voice = narrator_voice if speaker == "NARRATOR" else expert_voice
                    clip_bytes = tts_synthesize(text, voice=voice)
                    if not clip_bytes:
                        raise RuntimeError(f"TTS returned empty audio for turn {idx} speaker={speaker}")
                    clip_path = os.path.join(td, f"clip_{idx:04d}.mp3")
                    with open(clip_path, "wb") as f:
                        f.write(clip_bytes)
                    clip_paths.append(clip_path)

                out_mp3 = os.path.join(td, "podcast.mp3")
                concat_mp3_files(clip_paths, out_mp3)  # versione re-encode
                audio_mp3 = open(out_mp3, "rb").read()
        else:
            audio_mp3 = tts_synthesize(script, voice=narrator_voice)

    # 4) Upload opzionale (per demo puoi spegnerlo)
    store = (os.getenv("STORE_OUTPUTS", "0").strip() == "1")
    script_url = ""
    mp3_url = ""
    if store:
        out_id = str(uuid.uuid4())
        script_key = f"outputs/{user_id}/{out_id}.txt"
        mp3_key = f"outputs/{user_id}/{out_id}.mp3"
        s3_put_bytes(script_key, script.encode("utf-8"), "text/plain; charset=utf-8")
        script_url = public_file_url(script_key)
        if audio_mp3:
            s3_put_bytes(mp3_key, audio_mp3, "audio/mpeg")
            mp3_url = public_file_url(mp3_key)

    return {
        "transcript": transcript,
        "script": script,
        "mp3_bytes": audio_mp3,
        "script_url": script_url,
        "mp3_url": mp3_url,
    }

# -----------------------------
# Stripe: checkout URL
# -----------------------------
def stripe_checkout_url(customer_ref: str) -> str:
    """
    Crea una Stripe Checkout session (subscription).
    customer_ref: stringa che identifichi l'utente (es: tg:chat_id)
    """
    if not (S.stripe_secret_key and S.stripe_price_id):
        raise RuntimeError("Stripe not configured: set STRIPE_SECRET_KEY and STRIPE_PRICE_ID")

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": S.stripe_price_id, "quantity": 1}],
        success_url=S.stripe_success_url + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=S.stripe_cancel_url,
        client_reference_id=customer_ref,
    )
    return session.url


# -----------------------------
# FastAPI app
# -----------------------------
app = FastAPI(title="Podcast Generator (Telegram)", version="0.1.0")


@app.get("/healthz")
def healthz():
    """
    Healthcheck semplice.
    Utile per capire env e che l'app è viva.
    """
    return {
        "ok": True,
        "env": S.env,
        "ts": now_ms(),
        "providers": {"llm": S.llm_provider, "stt": S.stt_provider, "tts": S.tts_provider},
        "data_dir": S.data_dir,
    }


@app.get("/files/{key:path}")
def files_proxy(key: str):
    """
    Proxy minimal per scaricare file da S3/MinIO.
    In prod, tipicamente useresti presigned URL o CDN.
    """
    try:
        data = s3_get_bytes(key)
    except Exception as e:
        raise HTTPException(404, f"Not found: {e}")

    # Content-Type molto semplice in base all'estensione
    if key.endswith(".txt"):
        return Response(content=data, media_type="text/plain; charset=utf-8")
    if key.endswith(".mp3"):
        return Response(content=data, media_type="audio/mpeg")

    return Response(content=data, media_type="application/octet-stream")


# -----------------------------
# Telegram webhook (solo bottoni)
# -----------------------------
@app.post("/webhooks/telegram/{secret}")
async def telegram_webhook(secret: str, req: Request):
    """
    Webhook Telegram:
    - gestisce /start
    - gestisce callback bottoni
    - gestisce testo + vocali
    - applica access control:
        * dev: access always
        * prod (se REQUIRE_SUBSCRIPTION=1): richiede subscribed:<chat_id>=1 su Redis
    """
    if secret != S.telegram_webhook_secret:
        raise HTTPException(403, "bad secret")

    update = await req.json()

    message = update.get("message") or {}
    callback = update.get("callback_query") or {}

    # ---------- CALLBACK (solo bottoni) ----------
    if callback:
        chat_id = str((callback.get("message") or {}).get("chat", {}).get("id", ""))
        data = str(callback.get("data", ""))

        pay_url = f"{S.public_base_url.rstrip('/')}/pay/tg/{chat_id}"

        if data == "help":
            tg_send_message(
                chat_id,
                "💡 Invia un testo o un vocale.\n"
                "Io genero uno script e (se TTS attivo) anche l’audio.\n",
                buttons=[
                    [{"text": "➕ Nuovo podcast", "callback_data": "new"}],
                    [{"text": "💳 Abbonati", "url": pay_url}],
                ],
            )
        elif data == "new":
            tg_send_message(
                chat_id,
                "Ok! Mandami ora testo oppure un vocale 🎙️",
                buttons=[[{"text": "❓ Aiuto", "callback_data": "help"}]],
            )
        else:
            tg_send_message(
                chat_id,
                "Azione non riconosciuta.",
                buttons=[[{"text": "❓ Aiuto", "callback_data": "help"}]],
            )

        return {"ok": True}

    # ---------- MESSAGE ----------
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if not chat_id:
        return {"ok": True}

    pay_url = f"{S.public_base_url.rstrip('/')}/pay/tg/{chat_id}"
    buttons_home = [
        [{"text": "➕ Nuovo podcast", "callback_data": "new"}],
        [{"text": "💳 Abbonati", "url": pay_url}],
        [{"text": "❓ Aiuto", "callback_data": "help"}],
        [{"text": "🧪 Demo Gradio", "url": f"{S.public_base_url.rstrip('/')}/demo"}],
    ]

    text = (message.get("text") or "").strip()

    # /start
    if text.lower().startswith("/start"):
        tg_send_message(
            chat_id,
            "Ciao! 🎧\n"
            "Mandami un testo o un vocale e creo un mini-podcast.\n\n"
            "In DEV puoi generare senza abbonarti.\n"
            "In PROD puoi attivare il paywall con REQUIRE_SUBSCRIPTION=1.",
            buttons=buttons_home,
        )
        return {"ok": True}

    # Access control (dev free)
    if not user_has_access(chat_id):
        tg_send_message(
            chat_id,
            "🔒 Per generare devi essere abbonato.\n"
            "Premi 'Abbonati' per completare il pagamento.",
            buttons=[[{"text": "💳 Abbonati", "url": pay_url}], [{"text": "❓ Aiuto", "callback_data": "help"}]],
        )
        return {"ok": True}

    # testo -> enqueue
    if text:
        tg_send_message(chat_id, "⏳ Ricevuto! Sto generando il podcast…", buttons=[[{"text": "❓ Aiuto", "callback_data": "help"}]])

        job = {
            "channel": "telegram",
            "user_id": f"tg-{chat_id}",
            "chat_id": chat_id,
            "input_text": text,
            "input_audio_s3_key": "",
        }
        q.enqueue("worker.process_job", job)
        return {"ok": True}

    # vocale -> download -> salva su /data -> upload su S3 -> enqueue
    voice = message.get("voice")
    if voice and voice.get("file_id"):
        file_id = voice["file_id"]

        tg_send_message(chat_id, "🎙️ Vocale ricevuto! Sto trascrivendo e generando…")

        audio_bytes, file_path = tg_get_file_bytes(file_id)

        # Persistiamo su volume /data per debug/caching (richiesta utente)
        local_name = f"{uuid.uuid4()}_{os.path.basename(file_path)}"
        local_path = os.path.join(S.data_dir, local_name)
        with open(local_path, "wb") as f:
            f.write(audio_bytes)

        in_key = f"inputs/tg-{chat_id}/{uuid.uuid4()}.ogg"
        s3_put_bytes(in_key, audio_bytes, "audio/ogg")

        job = {
            "channel": "telegram",
            "user_id": f"tg-{chat_id}",
            "chat_id": chat_id,
            "input_text": "",
            "input_audio_s3_key": in_key,
        }
        q.enqueue("worker.process_job", job)
        return {"ok": True}

    # fallback
    tg_send_message(chat_id, "Formato non riconosciuto. Invia testo o vocale.", buttons=[[{"text": "❓ Aiuto", "callback_data": "help"}]])
    return {"ok": True}


# -----------------------------
# Stripe endpoints
# -----------------------------
@app.get("/pay/tg/{chat_id}")
def pay_tg(chat_id: str):
    """
    Checkout Stripe:
    - In dev puoi anche ignorarlo (REQUIRE_SUBSCRIPTION=0).
    - In prod lo userai per abilitare accesso dopo pagamento.

    Qui facciamo redirect a Stripe per UX migliore da bottone Telegram.
    """
    try:
        url = stripe_checkout_url(customer_ref=f"tg:{chat_id}")
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=url, status_code=302)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/stripe/success")
def stripe_success(session_id: str = ""):
    """
    Success page:
    - recupera la Checkout Session
    - prende client_reference_id (tg:<chat_id>)
    - setta subscribed:<chat_id>=1 su Redis

    Così:
    - DEV (REQUIRE_SUBSCRIPTION=0): non serve ma non fa male
    - PROD (REQUIRE_SUBSCRIPTION=1): abilita davvero l’utente
    """
    if not session_id:
        return PlainTextResponse("✅ Pagamento completato. (session_id mancante)")

    if not S.stripe_secret_key:
        return PlainTextResponse("✅ Pagamento completato. (Stripe key non configurata sul server)")

    try:
        session = stripe.checkout.Session.retrieve(session_id)
        ref = session.get("client_reference_id", "") or ""
        # atteso: "tg:<chat_id>"
        if ref.startswith("tg:"):
            chat_id = ref.split("tg:", 1)[1]
            redis_conn.set(f"subscribed:{chat_id}", "1")
        return PlainTextResponse("✅ Pagamento completato. Ora puoi tornare su Telegram e generare podcast.")
    except Exception as e:
        return PlainTextResponse(f"✅ Pagamento completato. (errore post-processing: {e})")

@app.get("/stripe/cancel")
def stripe_cancel():
    """Pagina minimale cancel."""
    return PlainTextResponse("Pagamento annullato. Puoi riprovare dal bot.")


# -----------------------------
# Gradio demo (mounted)
# -----------------------------
def gradio_generate(text: str, audio_file, user_id: str):
    """
    Gradio handler:
    - se c'è audio: lo carica su S3 e usa SOLO STT
    - altrimenti usa text
    - mostra transcript e script
    - riproduce audio in Gradio
    """
    import os
    import uuid

    user_id = (user_id or "demo").strip() or "demo"

    audio_key = ""
    source_text = (text or "").strip()

    # Se ho audio, ignoro il testo (evita "tema di default" / placeholder)
    if audio_file:
        source_text = ""

        # audio_file è filepath (type="filepath")
        with open(audio_file, "rb") as f:
            audio_bytes = f.read()

        # Salviamo il vocale su S3/MinIO così la pipeline lo scarica e fa STT
        audio_key = f"inputs/{user_id}/{uuid.uuid4()}.ogg"
        s3_put_bytes(audio_key, audio_bytes, "audio/ogg")

    out = run_podcast_pipeline(
        user_id=user_id,
        input_text=source_text,
        input_audio_s3_key=audio_key,
    )

    transcript = out.get("transcript", "")
    script = out.get("script", "")
    mp3_bytes = out.get("mp3_bytes", b"")

    audio_path = ""
    if mp3_bytes:
        os.makedirs("/data/gradio_audio", exist_ok=True)
        audio_path = f"/data/gradio_audio/{user_id}_{uuid.uuid4()}.mp3"
        with open(audio_path, "wb") as f:
            f.write(mp3_bytes)

    # Outputs: transcript, script, audio
    return transcript, script, audio_path

with gr.Blocks() as demo:
    gr.Markdown("# Podcast Generator (Narratrice + Esperto)")

    user_id_in = gr.Textbox(value="demo", label="User ID")

    text_in = gr.Textbox(lines=6, label="Testo (opzionale, ignorato se invii un vocale)", value="")
    audio_in = gr.Audio(sources=["microphone", "upload"], type="filepath", label="Vocale (opzionale)")

    btn = gr.Button("Genera Podcast")

    transcript_out = gr.Textbox(lines=6, label="Transcript (STT)")
    script_out = gr.Textbox(lines=14, label="Script generato")
    audio_out = gr.Audio(type="filepath", label="Audio (MP3)")

    btn.click(
        fn=gradio_generate,
        inputs=[text_in, audio_in, user_id_in],
        outputs=[transcript_out, script_out, audio_out],
    )

app = gr.mount_gradio_app(app, demo, path="/demo")


# -----------------------------
# Main entrypoint
# -----------------------------
def main():
    """
    Avvia uvicorn.
    In dev: reload True per sviluppo rapido.
    In prod: reload False.
    """
    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8080,
        reload=(S.env == "dev"),
    )


if __name__ == "__main__":
    main()

