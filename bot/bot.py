import os
import random
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
K_MODE = "mode"          # "story" | "podcast"
K_WAITING = "waiting"    # "topic" | None
K_LANG = "lang"          # "Italian"

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
    ]
    podcast = [
        "Perché ci affezioniamo alle serie e ai personaggi.",
        "IA e creatività: cosa significa davvero 'originale'?",
        "Tecnologia e attenzione: perché ci distraiamo così tanto?",
        "Come costruire abitudini che durano.",
        "Cosa rende una voce autorevole in un podcast.",
    ]
    return random.choice(story if mode == "story" else podcast)

def build_podcast_dialogue(topic: str) -> str:
    # Template semplice (finché non aggiungi /gen/podcast)
    return (
        f"A: Benvenuti! Oggi parliamo di: {topic}\n"
        f"B: Interessante. Da dove partiamo?\n"
        f"A: Partiamo da un esempio concreto, semplice e quotidiano.\n"
        f"B: E poi vediamo cosa significa davvero e perché ci riguarda.\n"
        f"A: Esatto. Facciamo ordine e tiriamo fuori 3 idee utili.\n"
        f"B: Perfetto. Andiamo!"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[K_MODE] = "story"
    context.user_data[K_WAITING] = None
    context.user_data[K_LANG] = "Italian"

    await update.message.reply_text(
        "Scegli cosa vuoi creare e come scegliere l'argomento.\n\n"
        "📖 Storia = monologo (Voce A)\n"
        "🎙️ Podcast = dialogo A/B\n\n"
        "Poi scegli: ✍️ scrivi argomento oppure 🎲 casuale.",
        reply_markup=kb_main(context.user_data[K_LANG]),
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    dbg(logger, f"[BOT] button {data}")

    lang = context.user_data.get(K_LANG, "Italian")

    if data.startswith("mode:"):
        _, mode = data.split(":", 1)
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
            f"🎲 Argomento casuale:\n\n{topic}\n\n⏳ Genero audio...",
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

    waiting = context.user_data.get(K_WAITING)
    if waiting != "topic":
        # Se l'utente scrive senza premere "Scrivo argomento", lo consideriamo comunque argomento
        context.user_data[K_WAITING] = None
        await update.message.reply_text(
            "✅ Ricevuto. Lo considero come argomento.\n⏳ Genero audio..."
        )
        await generate_and_send_voice(update, context, text)
        return

    # era in attesa di argomento
    context.user_data[K_WAITING] = None
    await update.message.reply_text("✅ Argomento ricevuto.\n⏳ Genero audio...")
    await generate_and_send_voice(update, context, text)

async def generate_and_send_voice(update: Update, context: ContextTypes.DEFAULT_TYPE, topic: str):
    mode = context.user_data.get(K_MODE, "story")
    lang = context.user_data.get(K_LANG, "Italian")

    try:
        if mode == "story":
            payload = {"text": topic, "language": lang, "speaker": "A"}
            endpoint = f"{API_URL}/voice/mono"
            timeout = 240
        else:
            dialogue_text = build_podcast_dialogue(topic)
            payload = {"text": dialogue_text, "language": lang, "pause_s": 0.18}
            endpoint = f"{API_URL}/voice/dialogue"
            timeout = 360

        dbg(logger, f"[BOT] request {endpoint} mode={mode} chars={len(payload['text'])}")
        r = requests.post(endpoint, json=payload, timeout=timeout)
        r.raise_for_status()

        # Risposta come voice message
        await update.effective_chat.send_voice(voice=r.content)

    except Exception as e:
        logger.exception("generate_and_send_voice failed")
        await update.effective_chat.send_message(f"❌ Errore: {e}")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    dbg(logger, "[BOT] starting polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
