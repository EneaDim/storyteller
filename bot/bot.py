import os
import random
import time
import asyncio
from io import BytesIO

import requests
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

from app.logging_setup import setup_logging, dbg

load_dotenv()
logger = setup_logging("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_URL = os.environ["API_URL"].rstrip("/")
if not API_URL.startswith(("http://", "https://")):
    API_URL = "https://" + API_URL

# ---- state keys ----
K_MODE = "mode"          # story | podcast
K_WAITING = "waiting"    # topic | None
K_LANG = "lang"
K_BUSY = "busy"


def kb_main(lang: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📖 Storia", callback_data="mode:story"),
            InlineKeyboardButton("🎙️ Podcast", callback_data="mode:podcast"),
        ],
        [
            InlineKeyboardButton("✍️ Scrivo un argomento", callback_data="topic:write"),
            InlineKeyboardButton("🎲 Argomento casuale", callback_data="topic:random"),
        ],
        [
            InlineKeyboardButton(f"🌐 Lingua: {lang}", callback_data="lang:Italian"),
        ],
    ])


def random_topic(mode: str) -> str:
    story = [
        "Una chiave che apre una porta che non dovrebbe esistere.",
        "Un messaggio vocale da un numero impossibile.",
        "Una città dove nessuno può mentire per 24 ore.",
    ]
    podcast = [
        "Perché ci affezioniamo alle serie.",
        "IA e creatività: cosa significa originale?",
        "Come costruiamo abitudini che durano.",
    ]
    return random.choice(story if mode == "story" else podcast)


def build_podcast_dialogue(topic: str) -> str:
    return (
        f"A: Benvenuti! Oggi parliamo di {topic}\n"
        f"B: Tema interessante. Perché conta?\n"
        f"A: Partiamo da un esempio concreto.\n"
        f"B: Poi tiriamo fuori tre idee chiave.\n"
        f"A: E chiudiamo con un consiglio pratico."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.update({
        K_MODE: "story",
        K_WAITING: None,
        K_LANG: "Italian",
        K_BUSY: False,
    })

    await update.message.reply_text(
        "Scegli cosa vuoi creare.\n"
        "📖 Storia = monologo\n"
        "🎙️ Podcast = dialogo A/B",
        reply_markup=kb_main("Italian"),
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    lang = context.user_data.get(K_LANG, "Italian")

    if data.startswith("mode:"):
        mode = data.split(":", 1)[1]
        context.user_data[K_MODE] = mode
        await q.edit_message_text(
            f"Modalità impostata: {mode}",
            reply_markup=kb_main(lang),
        )
        return

    if data == "topic:write":
        context.user_data[K_WAITING] = "topic"
        await q.edit_message_text("Scrivi l'argomento:", reply_markup=kb_main(lang))
        return

    if data == "topic:random":
        topic = random_topic(context.user_data.get(K_MODE, "story"))
        await q.edit_message_text(f"🎲 {topic}\n⏳ Genero audio…")
        await generate_and_send_voice(update, context, topic)
        return


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    context.user_data[K_WAITING] = None
    await update.message.reply_text("⏳ Genero audio…")
    await generate_and_send_voice(update, context, text)


async def generate_and_send_voice(update: Update, context: ContextTypes.DEFAULT_TYPE, topic: str):
    if context.user_data.get(K_BUSY):
        await update.effective_chat.send_message("⏳ Sto già generando, attendi…")
        return

    context.user_data[K_BUSY] = True
    mode = context.user_data.get(K_MODE, "story")
    lang = context.user_data.get(K_LANG, "Italian")

    try:
        if mode == "story":
            endpoint = f"{API_URL}/voice/mono"
            payload = {"text": topic, "language": lang, "speaker": "A"}
        else:
            endpoint = f"{API_URL}/voice/dialogue"
            payload = {"text": build_podcast_dialogue(topic), "language": lang}

        def _do_request():
            last = None
            for i in range(4):
                r = requests.post(endpoint, json=payload, timeout=(10, 600))
                if r.status_code == 503:
                    last = r
                    time.sleep(3 * (i + 1))
                    continue
                r.raise_for_status()
                return r.content
            if last:
                last.raise_for_status()

        audio_bytes = await asyncio.to_thread(_do_request)

        bio = BytesIO(audio_bytes)
        bio.name = "voice.ogg"
        await update.effective_chat.send_voice(voice=bio)

    except Exception as e:
        logger.exception("generate_and_send_voice failed")
        await update.effective_chat.send_message(f"❌ Errore: {e}")

    finally:
        context.user_data[K_BUSY] = False


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    dbg(logger, "[BOT] starting polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
