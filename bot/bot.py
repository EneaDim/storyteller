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
        "Una biblioteca dove i libri cambiano testo quando li chiudi.",
        "Un messaggio vocale da un numero impossibile.",
        "Un ascensore che si ferma a un piano non segnato.",
        "Una città dove nessuno può mentire per 24 ore.",
        "Un treno notturno che arriva sempre con 10 minuti di anticipo… ma solo per te.",
        "Un vecchio telefono pubblico che squilla una volta al mese.",
        "Un pacco senza mittente che contiene una foto di domani.",
        "Una radio che trasmette notizie di eventi che non sono ancora accaduti.",
        "Una mappa che cambia ogni volta che la guardi, ma ti porta sempre nello stesso posto.",
        "Un bar dove ogni cliente sembra conoscerti, tranne te.",
        "Una moneta che cade sempre in piedi e porta guai a chi la raccoglie.",
        "Un cane randagio che ti guida in strade che non esistono sulle mappe.",
        "Una stanza in casa tua che appare solo quando sei molto stanco.",
        "Un biglietto del cinema per un film che non è mai stato distribuito.",
        "Una lettera scritta da te… ma con una calligrafia che non riconosci.",
        "Un numero in rubrica chiamato “Io (tra 10 anni)”.",
        "Un sogno ricorrente che inizia a lasciare tracce nella realtà.",
        "Un vicino che cambia volto ogni settimana, ma nessuno se ne accorge.",
        "Una fotografia che mostra persone che non erano presenti in quel giorno.",
    ]
    podcast = [
        "Perché ci affezioniamo alle serie e ai personaggi.",
        "IA e creatività: cosa significa davvero “originale”?",
        "Tecnologia e attenzione: perché ci distraiamo così tanto?",
        "Come costruire abitudini che durano.",
        "Cosa rende una voce autorevole in un podcast.",
        "Perché procrastiniamo anche quando sappiamo cosa fare.",
        "Come si crea fiducia: nelle persone e nei prodotti.",
        "Minimalismo digitale: è davvero possibile?",
        "Il valore del silenzio in un mondo rumoroso.",
        "Imparare velocemente: tecniche che funzionano davvero.",
        "Rituali quotidiani: superstizione o psicologia pratica?",
        "Ansia da performance: come si affronta in modo sano.",
        "Perché il feedback fa male (e come usarlo bene).",
        "L’effetto “sempre reperibile”: costo nascosto dei messaggi.",
        "La motivazione è sopravvalutata? Parliamo di sistemi.",
        "Essere creativi senza ispirazione: metodi e struttura.",
        "L’arte di dire no senza sentirsi in colpa.",
        "La nostalgia: perché ci piace (e quando ci blocca).",
        "Dopamina e abitudini: cosa c’è di vero?",
        "Come prendere decisioni migliori con meno stress.",
    ]
    return random.choice(story if mode == "story" else podcast)


def build_podcast_dialogue(topic: str) -> str:
    return (
        f"A: Benvenuti! Oggi parliamo di {topic}\n"
        f"B: Tema interessante. Perché conta davvero nella vita di tutti i giorni?\n"
        f"A: Partiamo da un esempio concreto e molto semplice.\n"
        f"B: Poi tiriamo fuori tre idee chiave e un consiglio pratico.\n"
        f"A: E chiudiamo con una sintesi chiara, da portarsi a casa."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.update({
        K_MODE: "story",
        K_WAITING: None,
        K_LANG: "Italian",
        K_BUSY: False,
    })

    await update.message.reply_text(
        "Scegli cosa vuoi creare e come scegliere l’argomento.\n\n"
        "📖 Storia = monologo\n"
        "🎙️ Podcast = dialogo A/B\n",
        reply_markup=kb_main(context.user_data[K_LANG]),
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    dbg(logger, f"[BOT] button {data}")

    lang = context.user_data.get(K_LANG, "Italian")

    if data.startswith("mode:"):
        mode = data.split(":", 1)[1]
        context.user_data[K_MODE] = mode
        context.user_data[K_WAITING] = None
        await q.edit_message_text(
            f"✅ Modalità: {'Storia' if mode=='story' else 'Podcast'}\n"
            "Ora scegli: ✍️ scrivi argomento oppure 🎲 casuale.",
            reply_markup=kb_main(lang),
        )
        return

    if data == "topic:write":
        context.user_data[K_WAITING] = "topic"
        mode = context.user_data.get(K_MODE, "story")
        await q.edit_message_text(
            f"✍️ Scrivi ora l'argomento per {'la storia' if mode=='story' else 'il podcast'}.\n"
            "Esempio: “Una storia breve ambientata in una metropolitana notturna”.",
            reply_markup=kb_main(lang),
        )
        return

    if data == "topic:random":
        mode = context.user_data.get(K_MODE, "story")
        topic = random_topic(mode)
        await q.edit_message_text(
            f"🎲 Argomento scelto:\n\n{topic}\n\n⏳ Genero audio...",
            reply_markup=kb_main(lang),
        )
        await generate_and_send_voice(update, context, topic)
        return

    if data.startswith("lang:"):
        _, lang_val = data.split(":", 1)
        context.user_data[K_LANG] = lang_val
        await q.edit_message_text(
            f"✅ Lingua impostata: {lang_val}",
            reply_markup=kb_main(lang_val),
        )
        return


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    mode = context.user_data.get(K_MODE, "story")
    lang = context.user_data.get(K_LANG, "Italian")

    # Se non era in attesa, consideriamo comunque il testo come argomento
    context.user_data[K_WAITING] = None

    await update.message.reply_text(
        f"📝 Argomento scelto:\n\n{text}\n\n⏳ Genero audio ({'Storia' if mode=='story' else 'Podcast'})...",
        reply_markup=kb_main(lang),
    )
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
            payload = {"text": build_podcast_dialogue(topic), "language": lang, "pause_s": 0.18}

        dbg(logger, f"[BOT] request {endpoint} mode={mode} chars={len(payload['text'])}")

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
            return b""

        audio_bytes = await asyncio.to_thread(_do_request)
        if not audio_bytes:
            raise RuntimeError("API returned empty body (0 bytes).")

        bio = BytesIO(audio_bytes)
        bio.name = "voice.ogg"
        await update.effective_chat.send_voice(voice=bio)

        # Dopo il vocale: ripresenta il menu come /start
        await update.effective_chat.send_message(
            "Vuoi crearne un’altra? Scegli modalità e argomento:",
            reply_markup=kb_main(lang),
        )

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
