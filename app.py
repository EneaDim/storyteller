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

import asyncio
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
PodcastLang = Literal["it", "en"]

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
    telegram_mode: str = os.getenv("TELEGRAM_MODE", "webhook").strip().lower()  # "polling" | "webhook"
    telegram_poll_timeout: int = int(os.getenv("TELEGRAM_POLL_TIMEOUT", "30"))
    telegram_poll_sleep: float = float(os.getenv("TELEGRAM_POLL_SLEEP", "1"))
    telegram_offset_key: str = os.getenv("TELEGRAM_OFFSET_KEY", "tg_offset").strip() or "tg_offset"

    # Stripe
    stripe_secret_key: str = os.getenv("STRIPE_SECRET_KEY", "")
    stripe_price_id: str = os.getenv("STRIPE_PRICE_ID", "")
    stripe_success_url: str = os.getenv("STRIPE_SUCCESS_URL", "http://localhost:8080/stripe/success")
    stripe_cancel_url: str = os.getenv("STRIPE_CANCEL_URL", "http://localhost:8080/stripe/cancel")

    # Lingua di default del podcast (override per-chat in Redis, override per-job nel payload)
    podcast_default_lang: str = os.getenv("PODCAST_DEFAULT_LANG", "it").strip().lower()  # "it" | "en"

    # STT: se vuoi forzare lingua diversa dal podcast, setta STT_LANGUAGE.
    # Se STT_LANGUAGE="auto" => faster-whisper farà autodetect.
    stt_language_default: str = os.getenv("STT_LANGUAGE", "").strip().lower()  # "", "it", "en", "auto"

    # (Opzionale) prompt style / durata
    podcast_duration_hint: str = os.getenv("PODCAST_DURATION_HINT", "1–2 minutes").strip()

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

def tg_api_get(method: str, params: Optional[dict] = None) -> Dict[str, Any]:
    """
    GET wrapper per Telegram Bot API (utile per getUpdates).
    """
    if not S.telegram_bot_token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{S.telegram_bot_token}/{method}"
    r = requests.get(url, params=params or {}, timeout=90)
    if r.status_code >= 400:
        raise RuntimeError(f"Telegram API GET error {r.status_code}: {r.text}")
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API returned not ok: {data}")
    return data

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

# -----------------------------
# Prosody markup parsing (LLM tags)
# -----------------------------
import re
from dataclasses import dataclass
from typing import List, Literal, Union

ProsodySpeed = Literal["slow", "normal", "fast"]
ProsodySfx = Literal["breath", "sigh", "laugh"]

@dataclass
class Say:
    text: str
    speed: ProsodySpeed

@dataclass
class Pause:
    ms: int

@dataclass
class Sfx:
    kind: ProsodySfx

ProsodyAction = Union[Say, Pause, Sfx]

_TAG_RE = re.compile(r"\[(PAUSE=\d+|BREATH|SIGH|LAUGH|SLOW|NORMAL|FAST)\]")

def parse_prosody_actions(utterance: str, default_speed: ProsodySpeed = "normal") -> List[ProsodyAction]:
    """
    Parse a single utterance containing tags into actions.
    Supported tags:
      [PAUSE=ms] (0..2000 clamped)
      [BREATH] [SIGH] [LAUGH]
      [SLOW] [NORMAL] [FAST]  (stateful)
    """
    s = (utterance or "").strip()
    if not s:
        return []

    actions: List[ProsodyAction] = []
    speed: ProsodySpeed = default_speed

    parts = _TAG_RE.split(s)  # [text, tag, text, tag, ...]
    for part in parts:
        if not part:
            continue
        part = part.strip()
        if not part:
            continue

        # Tag handling
        if part.startswith("PAUSE="):
            try:
                ms = int(part.split("=", 1)[1])
                ms = max(0, min(ms, 2000))
                actions.append(Pause(ms=ms))
            except Exception:
                pass
            continue

        if part in ("BREATH", "SIGH", "LAUGH"):
            actions.append(Sfx(kind=part.lower()))  # type: ignore[arg-type]
            continue

        if part in ("SLOW", "NORMAL", "FAST"):
            speed = {"SLOW": "slow", "NORMAL": "normal", "FAST": "fast"}[part]  # type: ignore[assignment]
            continue

        # Text chunk
        actions.append(Say(text=part, speed=speed))

    # Merge adjacent Say with same speed
    merged: List[ProsodyAction] = []
    for a in actions:
        if merged and isinstance(a, Say) and isinstance(merged[-1], Say) and a.speed == merged[-1].speed:
            merged[-1].text = (merged[-1].text.rstrip() + " " + a.text.lstrip()).strip()
        else:
            merged.append(a)

    return merged

def _speed_to_length_scale(speed: str) -> float:
    # Piper: smaller length_scale => faster speech; larger => slower
    return {"slow": 1.15, "normal": 1.0, "fast": 0.88}.get(speed, 1.0)

def tts_synthesize_with_speed(text: str, voice: str, lang: str, speed: str) -> bytes:
    """
    Same as tts_synthesize(), but passes length_scale when using piper_http.
    Falls back to normal tts_synthesize for other providers.
    """
    import os

    text = (text or "").strip()
    if not text:
        return b""

    lang = normalize_lang(lang)
    provider = (getattr(S, "tts_provider", "") or os.getenv("TTS_PROVIDER", "coqui_stub")).strip().lower()

    if provider != "piper_http":
        return tts_synthesize(text, voice=voice, lang=lang)

    base = (os.getenv("PIPER_TTS_URL", "") or "").strip()
    fmt = (os.getenv("PIPER_TTS_FORMAT", "mp3") or "mp3").strip().lower()
    if not base:
        raise RuntimeError("TTS_PROVIDER=piper_http but PIPER_TTS_URL is empty")

    voice_id = (voice or "").strip() or get_available_voices(lang)[0]
    length_scale = _speed_to_length_scale(speed)

    payload = {
        "text": text,
        "voice_id": voice_id,
        "format": fmt,
        "lang": lang,
        "length_scale": length_scale,   # <-- requires piper_server support (next step if missing)
    }

    r = requests.post(f"{base.rstrip('/')}/tts", json=payload, timeout=240)
    if r.status_code != 200:
        raise RuntimeError(f"Piper TTS error {r.status_code}: {r.text[:1200]}")
    if not r.content:
        raise RuntimeError("Piper TTS returned empty audio bytes")
    return r.content

def make_silence_mp3(ms: int, out_path: str) -> None:
    """
    Generate silence MP3 of ms duration.
    Requires ffmpeg.
    """
    import subprocess

    ms = int(ms)
    ms = max(0, min(ms, 5000))
    dur = ms / 1000.0

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r=48000:cl=mono",
        "-t", f"{dur:.3f}",
        "-q:a", "4",
        "-codec:a", "libmp3lame",
        out_path,
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", errors="ignore")
        raise RuntimeError(f"ffmpeg silence failed: {err[:1600]}")

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

def normalize_lang(lang: str) -> str:
    """
    Normalizza codici lingua in "it" o "en".
    Accetta varianti comuni: "ita", "italian", "en_US", ecc.
    """
    x = (lang or "").strip().lower()
    if not x:
        return S.podcast_default_lang
    if x.startswith("it"):
        return "it"
    if x.startswith("en"):
        return "en"
    if x in ("italian", "ita"):
        return "it"
    if x in ("english", "eng"):
        return "en"
    return S.podcast_default_lang


def get_chat_lang(chat_id: str) -> str:
    """
    Lingua per-chat salvata su Redis: lang:<chat_id> = "it" | "en"
    Fallback: PODCAST_DEFAULT_LANG
    """
    if not chat_id:
        return S.podcast_default_lang
    key = f"lang:{chat_id}"
    try:
        val = redis_conn.get(key)
        v = (val or b"").decode("utf-8").strip().lower()
        if v in ("it", "en"):
            return v
    except Exception:
        pass
    return S.podcast_default_lang


def set_chat_lang(chat_id: str, lang: str) -> str:
    """
    Set lingua per-chat in Redis. Ritorna la lingua normalizzata.
    """
    lang2 = normalize_lang(lang)
    if chat_id:
        try:
            redis_conn.set(f"lang:{chat_id}", lang2)
        except Exception:
            pass
    return lang2

def _voice_pref_key(chat_id: str, lang: str, role: str) -> str:
    return f"tg:voice:{chat_id}:{lang}:{role}"  # role narrator|expert

def get_chat_voice_id(chat_id: str, lang: str, role: str, fallback: str) -> str:
    try:
        v = redis_conn.get(_voice_pref_key(chat_id, lang, role))
        s = (v or b"").decode("utf-8").strip()
        return s or fallback
    except Exception:
        return fallback

def set_chat_voice_id(chat_id: str, lang: str, role: str, voice_id: str) -> None:
    voice_id = (voice_id or "").strip()
    if voice_id:
        redis_conn.set(_voice_pref_key(chat_id, lang, role), voice_id)

# -----------------------------
# i18n (Telegram texts)
# -----------------------------
I18N = {
    "it": {
        "start": "Ciao! 🎧\nMandami un testo o un vocale e creo un mini-podcast.",
        "ask_input": "Ok! Mandami ora testo oppure un vocale 🎙️",
        "received": "⏳ Ricevuto! Sto generando il podcast…",
        "received_voice": "🎙️ Vocale ricevuto! Sto trascrivendo e generando…",
        "help": "💡 Invia un testo o un vocale.\nIo genero uno script e (se TTS attivo) anche l’audio.",
        "locked": "🔒 Per generare devi essere abbonato.",
        "lang_set": "✅ Lingua impostata: {label}\n\nMandami un testo o un vocale 🎙️",
        "unknown": "Azione non riconosciuta.",
        "bad_format": "Formato non riconosciuto. Invia testo o vocale.",
        "voices_title": "🎙️ Voci\nScegli cosa vuoi configurare:",
        "voices_role": "Scegli la voce per: {role_label}",
        "saved_voice": "✅ Voce salvata per {role_label}: {voice_id}",
        "preview_sent": "🎧 Anteprima voce: {voice_id}",
    },
    "en": {
        "start": "Hi! 🎧\nSend me text or a voice message and I’ll generate a mini-podcast.",
        "ask_input": "Great — now send text or a voice message 🎙️",
        "received": "⏳ Got it! Generating the podcast…",
        "received_voice": "🎙️ Voice received! Transcribing and generating…",
        "help": "💡 Send text or a voice message.\nI’ll generate a script and (if TTS is enabled) also the audio.",
        "locked": "🔒 You must be subscribed to generate podcasts.",
        "lang_set": "✅ Language set: {label}\n\nNow send text or a voice message 🎙️",
        "unknown": "Unknown action.",
        "bad_format": "Unsupported format. Send text or a voice message.",
        "voices_title": "🎙️ Voices\nWhat do you want to configure?",
        "voices_role": "Choose the voice for: {role_label}",
        "saved_voice": "✅ Saved voice for {role_label}: {voice_id}",
        "preview_sent": "🎧 Voice preview: {voice_id}",
    },
}

def t(chat_id: str, key: str, **kwargs) -> str:
    lang = get_chat_lang(chat_id)
    s = (I18N.get(lang, I18N["it"]).get(key) or I18N["it"][key])
    return s.format(**kwargs)

# -----------------------------
# Telegram API helpers
# -----------------------------
def build_pay_url(chat_id: str) -> str:
    """
    Ritorna URL assoluto per /pay se e solo se PUBLIC_BASE_URL è configurato e valido.
    In polling locale tipicamente torna "" e quindi NON mostriamo il bottone.
    """
    base = (S.public_base_url or "").strip()
    if base.startswith("http://") or base.startswith("https://"):
        return f"{base.rstrip('/')}/pay/tg/{chat_id}"
    return ""

def build_lang_keyboard(chat_id: str = "") -> list:
    return [[
        {"text": "🇮🇹 Italiano", "callback_data": "lang_it"},
        {"text": "🇬🇧 English", "callback_data": "lang_en"},
    ]]

def get_available_voices(lang: str) -> list[str]:
    """
    Voices shown in Telegram.
    Configure via env:
      PIPER_VOICES_IT="it_IT-paola-medium,it_IT-riccardo-x_low"
      PIPER_VOICES_EN="en_US-amy-medium,en_GB-alan-medium"
    """
    lang = normalize_lang(lang)
    raw = os.getenv("PIPER_VOICES_EN" if lang == "en" else "PIPER_VOICES_IT", "").strip()
    voices = [v.strip() for v in raw.split(",") if v.strip()]

    # fallback to something safe if env is empty
    if not voices:
        voices = ["en_US-amy-medium", "en_GB-alan-medium"] if lang == "en" else ["it_IT-paola-medium", "it_IT-riccardo-x_low"]
    return voices


def build_voice_menu_keyboard(chat_id: str) -> list:
    lang = get_chat_lang(chat_id)
    back = "⬅️ Back" if lang == "en" else "⬅️ Indietro"
    narrator = "🎙️ Narrator" if lang == "en" else "🎙️ Narratore"
    expert = "🧠 Expert" if lang == "en" else "🧠 Esperto"

    return [
        [{"text": narrator, "callback_data": "voices_role:narrator"}],
        [{"text": expert, "callback_data": "voices_role:expert"}],
        [{"text": back, "callback_data": "home"}],
    ]

def role_label(chat_id: str, role: str) -> str:
    lang = get_chat_lang(chat_id)
    if role == "expert":
        return "Expert" if lang == "en" else "Esperto"
    return "Narrator" if lang == "en" else "Narratore"


def build_voice_list_keyboard(chat_id: str, role: str) -> list:
    """
    Shows per-voice:
      - set voice (saves in Redis)
      - preview voice (enqueue preview job)
    """
    lang = get_chat_lang(chat_id)
    voices = get_available_voices(lang)
    preview = "▶️ Preview" if lang == "en" else "▶️ Anteprima"
    back = "⬅️ Back" if lang == "en" else "⬅️ Indietro"

    rows = []
    for v in voices:
        rows.append([
            {"text": f"✅ {v}", "callback_data": f"voice_set:{role}:{v}"},
            {"text": preview, "callback_data": f"voice_preview:{v}"},
        ])

    rows.append([{"text": back, "callback_data": "voices"}])
    return rows

def build_preview_text(chat_id: str) -> str:
    lang = get_chat_lang(chat_id)
    if lang == "en":
        return os.getenv("PREVIEW_TEXT_EN", "Hi! This is a voice preview for the podcast.").strip()
    return os.getenv("PREVIEW_TEXT_IT", "Ciao! Questa è una demo di voce per il podcast.").strip()

def build_home_keyboard(chat_id: str) -> list:
    lang = get_chat_lang(chat_id)
    btn_new = "➕ New podcast" if lang == "en" else "➕ Nuovo podcast"
    btn_voices = "🎙️ Voices" if lang == "en" else "🎙️ Voci"
    btn_sub = "💳 Subscribe" if lang == "en" else "💳 Abbonati"
    btn_help = "❓ Help" if lang == "en" else "❓ Aiuto"

    buttons: list = [
        [{"text": btn_new, "callback_data": "new"}],
        [{"text": btn_voices, "callback_data": "voices"}],
        *build_lang_keyboard(chat_id),  # ✅ now valid
        [{"text": btn_help, "callback_data": "help"}],
    ]
    pay_url = build_pay_url(chat_id)
    if pay_url:
        buttons.insert(2, [{"text": btn_sub, "url": pay_url}])
    return buttons

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
def llm_generate_script(input_text: str, lang: str = "") -> str:
    """
    Genera uno script podcast in IT o EN usando il provider scelto.
    Formato SEMPRE:
      NARRATOR: ...
      EXPERT: ...

    lang: "it" | "en" (fallback: PODCAST_DEFAULT_LANG)
    """
    lang = normalize_lang(lang)

    if lang == "en":
        system_msg = "You are a podcast scriptwriter. Output plain text only."
        prompt = (
            "Write a modern, engaging podcast dialogue in ENGLISH.\n"
            "MANDATORY constraints (if you violate them, the answer is wrong):\n"
            "1) Each line MUST be a separate utterance.\n"
            '2) Each line MUST start EXACTLY with "NARRATOR:" or "EXPERT:"\n'
            "3) BOTH voices must appear (at least 4 lines NARRATOR and 4 lines EXPERT).\n"
            "4) No markdown, no titles, dialogue only.\n"
            f"5) Spoken duration: {S.podcast_duration_hint}.\n"
            "6) Structure: opening hook, 3 sections, closing with a call-to-action.\n"
            "\n"
            "PROSODY TAGS (optional but encouraged, use intentionally, NOT randomly):\n"
            "- Tags are allowed ONLY inside a dialogue line, never as standalone lines.\n"
            "- Allowed tags only: [PAUSE=150..800], [BREATH], [SIGH], [LAUGH], [SLOW], [NORMAL], [FAST]\n"
            "- Speed tags are stateful until changed.\n"
            "- Per line limits: max 2 pauses, max 2 SFX tags, do not put tags back-to-back.\n"
            "- Use tags to improve natural delivery (e.g., short pause after a hook, breath before a long explanation).\n"
            "\n"
            "FORMAT EXAMPLE (example only, don't copy content):\n"
            "NARRATOR: ... [PAUSE=250] ...\n"
            "EXPERT: ... [BREATH] ...\n"
            "\n"
            f"INPUT:\n{(input_text or '').strip()}\n"
        )
        
    else:
        system_msg = "Rispondi sempre in italiano. Output solo testo."
        prompt = (
            "Sei un autore di podcast.\n"
            "Scrivi uno script moderno e coinvolgente in ITALIANO.\n"
            "Vincoli OBBLIGATORI (se non li rispetti, la risposta è considerata errata):\n"
            "1) Ogni battuta DEVE essere su una riga separata.\n"
            '2) Ogni riga DEVE iniziare ESATTAMENTE con "NARRATOR:" oppure "EXPERT:"\n'
            "3) Devono comparire ENTRAMBE le voci (almeno 4 righe NARRATOR e 4 righe EXPERT).\n"
            "4) Niente markdown, niente titoli, solo dialogo.\n"
            f"5) Durata parlata {S.podcast_duration_hint}.\n"
            "6) Struttura: hook iniziale, 3 sezioni, chiusura con call-to-action.\n"
            "\n"
            "TAG DI PROSODIA (opzionali ma consigliati, usali con intenzione, NON a caso):\n"
            "- I tag sono consentiti SOLO dentro la stessa riga di dialogo, mai su righe isolate.\n"
            "- Tag consentiti: [PAUSE=150..800], [BREATH], [SIGH], [LAUGH], [SLOW], [NORMAL], [FAST]\n"
            "- I tag di velocità restano attivi finché non cambi tag.\n"
            "- Limiti per riga: max 2 pause, max 2 SFX, niente tag consecutivi.\n"
            "- Usa i tag per migliorare la naturalezza (es. pausa breve dopo un hook, respiro prima di una spiegazione lunga).\n"
            "\n"
            "ESEMPIO DI FORMATO (solo esempio, non copiare il contenuto):\n"
            "NARRATOR: ... [PAUSE=250] ...\n"
            "EXPERT: ... [BREATH] ...\n"
            "\n"
            f"INPUT:\n{(input_text or '').strip()}\n"
        )
       

    # -------------------------
    # OLLAMA (via /api/chat)
    # -------------------------
    if S.llm_provider == "ollama":
        url = f"{S.ollama_base_url.rstrip('/')}/api/chat"
        payload = {
            "model": S.ollama_model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
            "options": {"temperature": 0.2},
        }

        r = requests.post(url, json=payload, timeout=180)
        r.raise_for_status()
        data = r.json()

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
        from google.genai import types
    
        api_key = (S.google_api_key or os.getenv("GEMINI_API_KEY", "")).strip()
        if not api_key:
            raise RuntimeError("Missing GOOGLE_API_KEY (or GEMINI_API_KEY) for Google GenAI")
    
        client = genai.Client(api_key=api_key)
    
        resp = client.models.generate_content(
            model=S.google_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_msg,
                temperature=0.2,
            ),
        )
    
        if getattr(resp, "text", None):
            return str(resp.text).strip()
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

def stt_transcribe(audio_bytes: bytes, mime: str = "audio/ogg", lang: str = "") -> str:
    """
    Speech-to-Text (STT).
    - faster-whisper locale: STT_PROVIDER=faster_whisper_local
    - lang: "it" | "en" (fallback: PODCAST_DEFAULT_LANG)
    - Se STT_LANGUAGE env è settato, prevale su lang (eccetto "", usa lang).
      Se STT_LANGUAGE="auto" => autodetect (language=None).
    """
    import os
    import tempfile
    import subprocess

    lang = normalize_lang(lang)

    provider = (getattr(S, "stt_provider", "") or os.getenv("STT_PROVIDER", "stub")).strip().lower()

    if provider in ("stub", "none", ""):
        return "Trascrizione (stub): abilita STT_PROVIDER=faster_whisper_local per trascrivere davvero."

    if provider == "faster_whisper_local":
        from faster_whisper import WhisperModel

        model_name = (os.getenv("STT_MODEL", "base") or "base").strip()

        # Priorità lingua:
        # 1) S.stt_language_default (STT_LANGUAGE) se valorizzato
        # 2) lang del podcast (it/en)
        # 3) se "auto" => language=None
        stt_lang = (S.stt_language_default or "").strip().lower()
        if not stt_lang:
            stt_lang = lang

        language_arg = None if stt_lang == "auto" else stt_lang

        global _FW_MODEL
        try:
            _FW_MODEL  # type: ignore[name-defined]
        except NameError:
            _FW_MODEL = None  # type: ignore[name-defined]

        if _FW_MODEL is None:  # type: ignore[name-defined]
            _FW_MODEL = WhisperModel(model_name, device="cpu", compute_type="int8")  # type: ignore[name-defined]

        with tempfile.TemporaryDirectory() as td:
            in_path = os.path.join(td, "in.bin")
            wav_path = os.path.join(td, "audio.wav")

            with open(in_path, "wb") as f:
                f.write(audio_bytes)

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

            segments, info = _FW_MODEL.transcribe(  # type: ignore[name-defined]
                wav_path,
                language=language_arg,
                vad_filter=True,
            )

            text_parts = []
            for seg in segments:
                t = (seg.text or "").strip()
                if t:
                    text_parts.append(t)

            return " ".join(text_parts).strip()

    if provider == "openai_whisper":
        raise RuntimeError("openai_whisper richiede OPENAI_API_KEY (a pagamento). In dev usa faster_whisper_local.")

    raise RuntimeError(f"Unknown STT_PROVIDER={provider}")

def tts_synthesize(text: str, voice: str = "", lang: str = "") -> bytes:
    """
    voice MUST be a real voice_id when using piper_http (e.g. it_IT-paola-medium)
    """
    import os

    text = (text or "").strip()
    if not text:
        return b""

    lang = normalize_lang(lang)
    provider = (getattr(S, "tts_provider", "") or os.getenv("TTS_PROVIDER", "coqui_stub")).strip().lower()

    if provider == "coqui_stub":
        return b""

    if provider == "piper_http":
        base = (os.getenv("PIPER_TTS_URL", "") or "").strip()
        fmt = (os.getenv("PIPER_TTS_FORMAT", "mp3") or "mp3").strip().lower()
        if not base:
            raise RuntimeError("TTS_PROVIDER=piper_http but PIPER_TTS_URL is empty")
        if fmt not in ("mp3", "wav"):
            raise RuntimeError("PIPER_TTS_FORMAT must be 'mp3' or 'wav'")

        voice_id = (voice or "").strip()
        if not voice_id:
            # safe fallback if caller forgot voice_id
            voices = get_available_voices(lang)
            voice_id = voices[0]

        payload = {
            "text": text,
            "voice_id": voice_id,   # ✅ correct
            "format": fmt,
            "lang": lang,
        }

        r = requests.post(f"{base.rstrip('/')}/tts", json=payload, timeout=240)
        if r.status_code != 200:
            raise RuntimeError(f"Piper TTS error {r.status_code}: {r.text[:1200]}")
        if not r.content:
            raise RuntimeError("Piper TTS returned empty audio bytes")
        return r.content

    if provider == "openai":
        raise RuntimeError("OpenAI TTS is paid. Use piper_http for local/free mode.")

    raise RuntimeError(f"Unknown TTS_PROVIDER={provider}")

# -----------------------------
# Job: pipeline podcast
# -----------------------------
def run_podcast_pipeline(
    user_id: str,
    input_text: str,
    input_audio_s3_key: str = "",
    lang: str = "",
    chat_id: str = ""
) -> Dict[str, Any]:
    """
    Pipeline unica (bilingue):
    1) Transcript:
       - se audio S3 => STT(lang)
       - altrimenti transcript = input_text
    2) Script:
       - LLM(lang) => dialogo "NARRATOR:" / "EXPERT:"
    3) Audio:
       - parse_dialogue_script => TTS per turni (lang) => concat MP3 ricodificando
    4) Upload opzionale su S3
    """
    import os
    import uuid
    import tempfile

    lang = normalize_lang(lang)

    # 1) Transcript
    transcript = (input_text or "").strip()

    if input_audio_s3_key:
        audio_bytes = s3_get_bytes(input_audio_s3_key)
        transcript = stt_transcribe(audio_bytes, mime="audio/ogg", lang=lang).strip()

        # No contenuto random: se STT fallisce/è troppo corto => errore
        if len(transcript) < 8:
            raise RuntimeError("STT vuoto o troppo corto: riprova con un vocale più chiaro o più lungo.")

    if not transcript:
        raise RuntimeError("Empty transcript: provide text or audio")

    # 2) Script (solo da transcript reale)
    script = llm_generate_script(transcript, lang=lang)
    if not script.strip():
        raise RuntimeError("LLM returned empty script")

    # 3) TTS
    narrator_voice = os.getenv("PODCAST_VOICE_NARRATOR", "female").strip().lower()
    expert_voice = os.getenv("PODCAST_VOICE_EXPERT", "male").strip().lower()

    audio_mp3: bytes = b""

    fallback_f_it = os.getenv("PIPER_VOICE_F_IT", os.getenv("PIPER_VOICE_F", "it_IT-paola-medium")).strip()
    fallback_m_it = os.getenv("PIPER_VOICE_M_IT", os.getenv("PIPER_VOICE_M", "it_IT-riccardo-x_low")).strip()
    fallback_f_en = os.getenv("PIPER_VOICE_F_EN", fallback_f_it).strip()
    fallback_m_en = os.getenv("PIPER_VOICE_M_EN", fallback_m_it).strip()
    
    if lang == "en":
        fb_narr = fallback_f_en
        fb_exp = fallback_m_en
    else:
        fb_narr = fallback_f_it
        fb_exp = fallback_m_it
    
    # Se ho chat_id, leggo preferenze; altrimenti fallback
    if chat_id:
        narrator_voice_id = get_chat_voice_id(chat_id, lang, "narrator", fb_narr)
        expert_voice_id = get_chat_voice_id(chat_id, lang, "expert", fb_exp)
    else:
        narrator_voice_id = fb_narr
        expert_voice_id = fb_exp

    if (getattr(S, "tts_provider", "") or "").strip().lower() != "coqui_stub":
        turns = parse_dialogue_script(script)

        # decide se dialogo o monologo
        is_dialogue = (len(turns) >= 2) and any(spk == "EXPERT" for spk, _ in turns)

        if is_dialogue:
            with tempfile.TemporaryDirectory() as td:
                clip_paths: list[str] = []
                SFX_MS = {"breath": 180, "sigh": 250, "laugh": 220}
                
                clip_index = 0
                for turn_idx, (speaker, turn_text) in enumerate(turns):
                    voice_id = narrator_voice_id if speaker == "NARRATOR" else expert_voice_id
                
                    actions = parse_prosody_actions(turn_text, default_speed="normal")
                
                    for a in actions:
                        if isinstance(a, Say):
                            clip_bytes = tts_synthesize_with_speed(a.text, voice=voice_id, lang=lang, speed=a.speed)
                            if not clip_bytes:
                                raise RuntimeError(f"TTS empty for turn {turn_idx} speaker={speaker}")
                            clip_path = os.path.join(td, f"clip_{clip_index:06d}.mp3")
                            with open(clip_path, "wb") as f:
                                f.write(clip_bytes)
                            clip_paths.append(clip_path)
                            clip_index += 1
                
                        elif isinstance(a, Pause):
                            clip_path = os.path.join(td, f"clip_{clip_index:06d}.mp3")
                            make_silence_mp3(a.ms, clip_path)
                            clip_paths.append(clip_path)
                            clip_index += 1
                
                        elif isinstance(a, Sfx):
                            # placeholder: silence for now
                            ms = SFX_MS.get(a.kind, 200)
                            clip_path = os.path.join(td, f"clip_{clip_index:06d}.mp3")
                            make_silence_mp3(ms, clip_path)
                            clip_paths.append(clip_path)
                            clip_index += 1

        else:
            # fallback: TTS dell’intero script (di solito meno naturale ma funziona)
            audio_mp3 = tts_synthesize(script, voice=narrator_voice, lang=lang)

    # 4) Upload opzionale
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
        "lang": lang,
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

        if data in ("lang_it", "lang_en"):
            new_lang = "it" if data == "lang_it" else "en"
            new_lang = set_chat_lang(chat_id, new_lang)
            label = "🇮🇹 Italiano" if new_lang == "it" else "🇬🇧 English"
            tg_send_message(
                chat_id,
                f"✅ Lingua impostata: {label}\n\nOra mandami un testo o un vocale 🎙️",
                buttons=[
                    [{"text": "➕ Nuovo podcast", "callback_data": "new"}],
                    [{"text": "🇮🇹 Italiano", "callback_data": "lang_it"}, {"text": "🇬🇧 English", "callback_data": "lang_en"}],
                    [{"text": "❓ Aiuto", "callback_data": "help"}],
                ],
            )
            return {"ok": True}

        if data == "help":
            cur = get_chat_lang(chat_id)
            cur_label = "🇮🇹 Italiano" if cur == "it" else "🇬🇧 English"
            tg_send_message(
                chat_id,
                "💡 Invia un testo o un vocale.\n"
                "Io genero uno script e (se TTS attivo) anche l’audio.\n"
                f"Lingua attuale: {cur_label}\n",
                buttons=[
                    [{"text": "➕ Nuovo podcast", "callback_data": "new"}],
                    [{"text": "🇮🇹 Italiano", "callback_data": "lang_it"}, {"text": "🇬🇧 English", "callback_data": "lang_en"}],
                    [{"text": "💳 Abbonati", "url": pay_url}],
                ],

            )
        elif data == "new":
            cur = get_chat_lang(chat_id)
            cur_label = "🇮🇹 Italiano" if cur == "it" else "🇬🇧 English"
            tg_send_message(
                chat_id,
                f"Ok! Mandami ora testo oppure un vocale 🎙️\n(Lingua: {cur_label})",
                buttons=[
                    [{"text": "🇮🇹 Italiano", "callback_data": "lang_it"}, {"text": "🇬🇧 English", "callback_data": "lang_en"}],
                    [{"text": "❓ Aiuto", "callback_data": "help"}],
                ],
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
        lang = get_chat_lang(chat_id)
        job = {
            "channel": "telegram",
            "user_id": f"tg-{chat_id}",
            "chat_id": chat_id,
            "input_text": text,
            "input_audio_s3_key": "",
            "lang": lang,
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
        lang = get_chat_lang(chat_id)
        job = {
            "channel": "telegram",
            "user_id": f"tg-{chat_id}",
            "chat_id": chat_id,
            "input_text": "",
            "input_audio_s3_key": in_key,
            "lang": lang,
        }
        q.enqueue("worker.process_job", job)
        return {"ok": True}

    # fallback
    tg_send_message(chat_id, "Formato non riconosciuto. Invia testo o vocale.", buttons=[[{"text": "❓ Aiuto", "callback_data": "help"}]])
    return {"ok": True}

# -----------------------------
# Telegram: helpers (ADD ONCE in app.py)
# -----------------------------
def tg_send_document_bytes(
    chat_id: str,
    doc_bytes: bytes,
    filename: str = "script.txt",
    caption: str = "",
    mime_type: str = "text/plain",
) -> None:
    """
    Invia un documento a Telegram come upload multipart (non serve URL pubblico).
    Perfetto per inviare lo script completo come .txt.
    """
    if not doc_bytes:
        raise RuntimeError("tg_send_document_bytes: empty bytes")

    payload = {"chat_id": chat_id, "caption": caption}
    files = {"document": (filename, doc_bytes, mime_type)}
    tg_api("sendDocument", payload=payload, files=files)

def tg_send_audio_bytes(chat_id: str, audio_bytes: bytes, filename: str = "podcast.mp3", caption: str = "") -> None:
    """
    Invia audio a Telegram come upload multipart (non serve URL pubblico).
    """
    if not audio_bytes:
        raise RuntimeError("tg_send_audio_bytes: empty audio bytes")

    payload = {"chat_id": chat_id, "caption": caption}
    files = {"audio": (filename, audio_bytes, "audio/mpeg")}
    tg_api("sendAudio", payload=payload, files=files)

def tg_send_voice_bytes(chat_id: str, mp3_bytes: bytes, caption: str = "") -> None:
    """
    Converte MP3 -> OGG/OPUS e invia come Telegram voice message (sendVoice).
    Richiede ffmpeg nel container.
    """
    import os
    import tempfile
    import subprocess

    if not mp3_bytes:
        raise RuntimeError("tg_send_voice_bytes: empty mp3 bytes")

    with tempfile.TemporaryDirectory() as td:
        in_mp3 = os.path.join(td, "in.mp3")
        out_ogg = os.path.join(td, "out.ogg")

        with open(in_mp3, "wb") as f:
            f.write(mp3_bytes)

        # Telegram voice = OGG/OPUS
        cmd = [
            "ffmpeg",
            "-y",
            "-i", in_mp3,
            "-vn",
            "-ac", "1",
            "-ar", "48000",
            "-c:a", "libopus",
            "-b:a", "32k",
            out_ogg,
        ]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode != 0:
            err = p.stderr.decode("utf-8", errors="ignore")
            raise RuntimeError(f"ffmpeg mp3->opus failed: {err[:1600]}")

        ogg_bytes = open(out_ogg, "rb").read()

    payload = {"chat_id": chat_id, "caption": caption}
    files = {"voice": ("voice.ogg", ogg_bytes, "audio/ogg")}
    tg_api("sendVoice", payload=payload, files=files)

def handle_telegram_update(update: Dict[str, Any]) -> None:
    callback = update.get("callback_query") or {}
    message = update.get("message") or {}

    # -------------------------
    # CALLBACK_QUERY (buttons)
    # -------------------------
    if callback:
        msg = callback.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        data = str(callback.get("data") or "").strip()
        if not chat_id:
            return

        # Home
        if data == "home":
            tg_send_message(chat_id, t(chat_id, "start"), buttons=build_home_keyboard(chat_id))
            return

        # Language
        if data in ("lang_it", "lang_en"):
            new_lang = "it" if data == "lang_it" else "en"
            new_lang = set_chat_lang(chat_id, new_lang)
            label = "🇮🇹 Italiano" if new_lang == "it" else "🇬🇧 English"
            tg_send_message(chat_id, t(chat_id, "lang_set", label=label), buttons=build_home_keyboard(chat_id))
            return

        # Help
        if data == "help":
            tg_send_message(chat_id, t(chat_id, "help"), buttons=build_home_keyboard(chat_id))
            return

        # New
        if data == "new":
            tg_send_message(chat_id, t(chat_id, "ask_input"), buttons=build_home_keyboard(chat_id))
            return

        # Voices main menu
        if data == "voices":
            tg_send_message(chat_id, t(chat_id, "voices_title"), buttons=build_voice_menu_keyboard(chat_id))
            return

        # Choose role menu
        if data.startswith("voices_role:"):
            role = data.split(":", 1)[1].strip()
            if role not in ("narrator", "expert"):
                tg_send_message(chat_id, t(chat_id, "unknown"), buttons=build_home_keyboard(chat_id))
                return
            tg_send_message(
                chat_id,
                t(chat_id, "voices_role", role_label=role_label(chat_id, role)),
                buttons=build_voice_list_keyboard(chat_id, role),
            )
            return

        # Save voice selection
        if data.startswith("voice_set:"):
            # voice_set:<role>:<voice_id>
            parts = data.split(":", 2)
            if len(parts) != 3:
                tg_send_message(chat_id, t(chat_id, "unknown"), buttons=build_home_keyboard(chat_id))
                return
            role, voice_id = parts[1].strip(), parts[2].strip()
            if role not in ("narrator", "expert"):
                tg_send_message(chat_id, t(chat_id, "unknown"), buttons=build_home_keyboard(chat_id))
                return

            lang = get_chat_lang(chat_id)
            set_chat_voice_id(chat_id, lang, role, voice_id)

            tg_send_message(
                chat_id,
                t(chat_id, "saved_voice", role_label=role_label(chat_id, role), voice_id=voice_id),
                buttons=build_voice_list_keyboard(chat_id, role),
            )
            return

        # Preview voice (enqueue)
        if data.startswith("voice_preview:"):
            voice_id = data.split(":", 1)[1].strip()
            lang = get_chat_lang(chat_id)
            preview_text = build_preview_text(chat_id)

            # short ack message (localized optional)
            if lang == "en":
                tg_send_message(chat_id, f"🎧 Preview: {voice_id}")
            else:
                tg_send_message(chat_id, f"🎧 Anteprima: {voice_id}")

            q.enqueue(
                "worker.process_preview_voice",
                {
                    "channel": "telegram",
                    "chat_id": chat_id,
                    "lang": lang,
                    "voice_id": voice_id,
                    "text": preview_text,
                },
            )
            return
        
        tg_send_message(chat_id, t(chat_id, "unknown"), buttons=build_home_keyboard(chat_id))
        return

    # -------------------------
    # MESSAGE (text / voice)
    # -------------------------
    chat_id = str((message.get("chat") or {}).get("id") or "")
    if not chat_id:
        return

    text = (message.get("text") or "").strip()

    if text.lower().startswith("/start"):
        tg_send_message(chat_id, t(chat_id, "start"), buttons=build_home_keyboard(chat_id))
        return

    if not user_has_access(chat_id):
        tg_send_message(chat_id, t(chat_id, "locked"), buttons=build_home_keyboard(chat_id))
        return

    if text:
        tg_send_message(chat_id, t(chat_id, "received"), buttons=[[{"text": "❓ Help" if get_chat_lang(chat_id) == "en" else "❓ Aiuto", "callback_data": "help"}]])
        lang = get_chat_lang(chat_id)
        q.enqueue("worker.process_job", {
            "channel": "telegram",
            "user_id": f"tg-{chat_id}",
            "chat_id": chat_id,
            "input_text": text,
            "input_audio_s3_key": "",
            "lang": lang,
        })
        return

    voice = message.get("voice") or {}
    file_id = voice.get("file_id")
    if file_id:
        tg_send_message(chat_id, t(chat_id, "received_voice"), buttons=[[{"text": "❓ Help" if get_chat_lang(chat_id) == "en" else "❓ Aiuto", "callback_data": "help"}]])
        audio_bytes, file_path = tg_get_file_bytes(file_id)

        local_name = f"{uuid.uuid4()}_{os.path.basename(file_path)}"
        local_path = os.path.join(S.data_dir, local_name)
        with open(local_path, "wb") as f:
            f.write(audio_bytes)

        in_key = f"inputs/tg-{chat_id}/{uuid.uuid4()}.ogg"
        s3_put_bytes(in_key, audio_bytes, "audio/ogg")
        lang = get_chat_lang(chat_id)

        q.enqueue("worker.process_job", {
            "channel": "telegram",
            "user_id": f"tg-{chat_id}",
            "chat_id": chat_id,
            "input_text": "",
            "input_audio_s3_key": in_key,
            "lang": lang,
        })
        return

    tg_send_message(chat_id, t(chat_id, "bad_format"), buttons=build_home_keyboard(chat_id))

async def telegram_polling_loop():
    """
    Long polling loop:
    - legge offset da Redis (persistente)
    - chiama getUpdates con timeout lungo
    - per ogni update chiama handle_telegram_update()
    """
    if not S.telegram_bot_token:
        log.warning("TELEGRAM_BOT_TOKEN missing: polling disabled")
        return

    # Persistiamo offset su Redis, così se riavvii non riprendi vecchi update
    offset_key = S.telegram_offset_key

    # Inizializza offset
    try:
        raw = redis_conn.get(offset_key)
        offset = int((raw or b"0").decode("utf-8") or "0")
    except Exception:
        offset = 0

    log.info("Telegram polling started. offset=%s timeout=%s", offset, S.telegram_poll_timeout)

    while True:
        try:
            data = tg_api_get(
                "getUpdates",
                params={"timeout": S.telegram_poll_timeout, "offset": offset, "allowed_updates": ["message", "callback_query"]},
            )
            updates = data.get("result") or []

            for upd in updates:
                upd_id = int(upd.get("update_id", 0))
                offset = max(offset, upd_id + 1)

                # salva offset subito (robusto)
                try:
                    redis_conn.set(offset_key, str(offset))
                except Exception:
                    pass

                # gestisci update (sincrona) senza bloccare troppo l'event loop
                await asyncio.to_thread(handle_telegram_update, upd)

            if not updates:
                await asyncio.sleep(S.telegram_poll_sleep)

        except Exception as e:
            log.exception("Polling error: %s", e)
            await asyncio.sleep(2.0)


@app.on_event("startup")
async def _startup_tasks():
    # Se siamo in modalità polling, avvia loop
    if S.telegram_mode == "polling":
        asyncio.create_task(telegram_polling_loop())
        log.info("Telegram mode=polling")
    else:
        log.info("Telegram mode=webhook")

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
def gradio_generate(text: str, audio_file, user_id: str, lang: str):
    """
    Gradio handler (bilingue):
    - se c'è audio: lo carica su S3 e usa SOLO STT
    - altrimenti usa text
    - mostra transcript e script
    - riproduce audio in Gradio
    """
    import os
    import uuid

    user_id = (user_id or "demo").strip() or "demo"
    lang = normalize_lang(lang)

    audio_key = ""
    source_text = (text or "").strip()

    if audio_file:
        source_text = ""
        with open(audio_file, "rb") as f:
            audio_bytes = f.read()
        audio_key = f"inputs/{user_id}/{uuid.uuid4()}.ogg"
        s3_put_bytes(audio_key, audio_bytes, "audio/ogg")

    out = run_podcast_pipeline(
        user_id=user_id,
        input_text=source_text,
        input_audio_s3_key=audio_key,
        lang=lang,
        chat_id=chat_id,
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

    return transcript, script, audio_path

with gr.Blocks() as demo:
    gr.Markdown("# Podcast Generator (Narratrice + Esperto)")

    user_id_in = gr.Textbox(value="demo", label="User ID")

    lang_in = gr.Dropdown(choices=["it", "en"], value=S.podcast_default_lang, label="Language (IT/EN)")

    text_in = gr.Textbox(lines=6, label="Testo (opzionale, ignorato se invii un vocale)", value="")
    audio_in = gr.Audio(sources=["microphone", "upload"], type="filepath", label="Vocale (opzionale)")

    btn = gr.Button("Genera Podcast")

    transcript_out = gr.Textbox(lines=6, label="Transcript (STT)")
    script_out = gr.Textbox(lines=14, label="Script generato")
    audio_out = gr.Audio(type="filepath", label="Audio (MP3)")
    btn.click(
        fn=gradio_generate,
        inputs=[text_in, audio_in, user_id_in, lang_in],
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

