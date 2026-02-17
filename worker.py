"""
RQ Worker (Telegram-only)
========================

Questo worker:
- ascolta la coda Redis/RQ
- esegue la pipeline (STT -> LLM -> TTS -> upload)
- invia messaggi e/o audio su Telegram

Se vuoi scalare:
- aumenti repliche del worker
- o separi pipeline in step (ma qui restiamo minimal).
"""

import os
import logging
import redis as redis_lib
from rq import Worker, Queue, Connection

# Importiamo dal modulo app:
# - Settings S
# - pipeline run_podcast_pipeline
# - funzioni telegram tg_send_message, tg_send_audio
from app import S, run_podcast_pipeline, tg_send_message, tg_send_audio
from app import tg_send_audio_bytes, tg_send_document_bytes
from app import tg_send_voice_bytes
from app import tts_synthesize

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("podcast_worker")

redis_conn = redis_lib.Redis.from_url(S.redis_url)

# -----------------------------
# worker.py (REWRITE COMPLETE process_job)
# -----------------------------
def _normalize_lang(lang: str) -> str:
    x = (lang or "").strip().lower()
    if x.startswith("en"):
        return "en"
    return "it"

def process_preview_voice(job: dict) -> dict:
    chat_id = str(job.get("chat_id") or "").strip()
    lang = _normalize_lang(job.get("lang") or "it")
    voice_id = str(job.get("voice_id") or "").strip()
    text = (job.get("text") or "").strip()

    if not chat_id or not voice_id or not text:
        return {"ok": False, "error": "missing chat_id/voice_id/text"}

    mp3_bytes = tts_synthesize(text, voice=voice_id, lang=lang)
    tg_send_voice_bytes(chat_id, mp3_bytes, caption="")

    return {"ok": True}

def process_job(job: dict) -> dict:
    """
    Job expected:
      {
        "channel": "telegram",
        "user_id": "tg-<chat_id>",
        "chat_id": "<chat_id>",
        "input_text": "...",
        "input_audio_s3_key": "inputs/...",
        "lang": "it" | "en"
      }
    """
    channel = (job.get("channel") or "").strip()
    chat_id = str(job.get("chat_id") or "").strip()
    user_id = str(job.get("user_id") or "unknown").strip()
    lang = _normalize_lang(job.get("lang") or "")

    # Run pipeline (IMPORTANT: pass chat_id so voice prefs apply)
    out = run_podcast_pipeline(
        user_id=user_id,
        input_text=job.get("input_text", "") or "",
        input_audio_s3_key=job.get("input_audio_s3_key", "") or "",
        lang=lang,
        chat_id=chat_id,  # ✅ enables per-chat narrator/expert selection
    )

    # If not Telegram, just return
    if channel != "telegram" or not chat_id:
        return out

    # -------------------------
    # i18n copy
    # -------------------------
    if lang == "en":
        title = "✅ Podcast ready!"
        caption = "🎧 Here is your podcast"
        audio_missing = "Audio not available (TTS disabled)."
        links_intro = "🔗 Links:"
        btn_new = "➕ New podcast"
        btn_help = "❓ Help"
        btn_voices = "🎙️ Voices"
        btn_sub = "💳 Subscribe"
        btn_it = "🇮🇹 Italian"
        btn_en = "🇬🇧 English"
    else:
        title = "✅ Podcast pronto!"
        caption = "🎧 Ecco il tuo podcast"
        audio_missing = "Audio non disponibile (TTS disabilitato)."
        links_intro = "🔗 Link:"
        btn_new = "➕ Nuovo podcast"
        btn_help = "❓ Aiuto"
        btn_voices = "🎙️ Voci"
        btn_sub = "💳 Abbonati"
        btn_it = "🇮🇹 Italiano"
        btn_en = "🇬🇧 English"

    # -------------------------
    # decide “public mode” vs “local mode”
    # -------------------------
    base = (S.public_base_url or "").strip()
    has_public_base = base.startswith("http://") or base.startswith("https://")

    script_url = (out.get("script_url") or "").strip()
    mp3_url = (out.get("mp3_url") or "").strip()
    mp3_bytes = out.get("mp3_bytes") or b""

    # -------------------------
    # buttons (no invalid URL)
    # Add Voices button
    # -------------------------
    buttons = [
        [{"text": btn_new, "callback_data": "new"}],
        [{"text": btn_voices, "callback_data": "voices"}],
        [{"text": btn_it, "callback_data": "lang_it"}, {"text": btn_en, "callback_data": "lang_en"}],
    ]
    if has_public_base:
        buttons.insert(2, [{"text": btn_sub, "url": f"{base.rstrip('/')}/pay/tg/{chat_id}"}])

    # -------------------------
    # main message
    # -------------------------
    msg = title

    # Public links only if public base + links exist
    if has_public_base and (script_url or mp3_url):
        msg += f"\n\n{links_intro}"
        if script_url:
            msg += f"\n📄 {script_url}"
        if mp3_url:
            msg += f"\n🎧 {mp3_url}"

    # If no audio in any form
    if not mp3_url and not mp3_bytes:
        msg += "\n\n" + audio_missing

    # -------------------------
    # send audio (prefer VOICE bytes)
    # -------------------------
    if mp3_bytes:
        tg_send_voice_bytes(chat_id, mp3_bytes, caption=caption)
    elif has_public_base and mp3_url:
        # fallback: download from public URL and send as voice
        import requests
        r = requests.get(mp3_url, timeout=120)
        r.raise_for_status()
        tg_send_voice_bytes(chat_id, r.content, caption=caption)

    tg_send_message(chat_id, msg, buttons=buttons)

    return out

def main():
    """
    Avvio del worker RQ.
    """
    with Connection(redis_conn):
        import socket, os
        worker_name = f"podcast_worker@{socket.gethostname()}:{os.getpid()}"
        worker = Worker([Queue(S.rq_queue)], name=worker_name)
        log.info("Worker started. Queue=%s", S.rq_queue)
        worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()

