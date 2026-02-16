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

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("podcast_worker")

redis_conn = redis_lib.Redis.from_url(S.redis_url)


def process_job(job: dict) -> dict:
    """
    Funzione chiamata dal worker per ogni job in coda.

    job atteso:
      {
        "channel": "telegram",
        "user_id": "tg-<chat_id>",
        "chat_id": "<chat_id>",
        "input_text": "...",
        "input_audio_s3_key": "inputs/..."
      }
    """
    # Estraiamo campi
    channel = job.get("channel", "")
    chat_id = str(job.get("chat_id", ""))
    user_id = job.get("user_id", "unknown")

    # Eseguiamo pipeline (può sollevare eccezioni: RQ le logga)
    out = run_podcast_pipeline(
        user_id=user_id,
        input_text=job.get("input_text", "") or "",
        input_audio_s3_key=job.get("input_audio_s3_key", "") or "",
    )

    # Invio risultati solo se Telegram
    if channel == "telegram" and chat_id:
        # Messaggio principale: solo bottoni, stile moderno/clean
        msg = (
            "✅ Podcast pronto!\n\n"
            f"📄 Script: {out['script_url']}\n"
        )

        if out.get("mp3_url"):
            msg += f"🎧 Audio: {out['mp3_url']}\n"
        else:
            msg += "🎧 Audio: (non disponibile — abilita TTS_PROVIDER=openai)\n"

        buttons = [
            [{"text": "➕ Nuovo podcast", "callback_data": "new"}],
            [{"text": "💳 Abbonati", "url": f"{S.public_base_url.rstrip('/')}/pay/tg/{chat_id}"}],
            [{"text": "❓ Aiuto", "callback_data": "help"}],
        ]

        tg_send_message(chat_id, msg, buttons=buttons)

        # Se abbiamo mp3, proviamo anche a inviarlo come audio (più user-friendly)
        if out.get("mp3_url"):
            tg_send_audio(chat_id, out["mp3_url"], caption="🎧 Ecco il tuo podcast")

    return out


def main():
    """
    Avvio del worker RQ.
    """
    with Connection(redis_conn):
        worker = Worker([Queue(S.rq_queue)], name="podcast_worker")
        log.info("Worker started. Queue=%s", S.rq_queue)
        worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()

