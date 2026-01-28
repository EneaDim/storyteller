import os
import time
import subprocess
import requests
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from app.logging_setup import setup_logging

load_dotenv()
log = setup_logging("telegram-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_URL = os.getenv("API_URL", "").rstrip("/")
APP_ENV = os.getenv("APP_ENV", "dev")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in .env")
if not API_URL:
    raise RuntimeError("API_URL missing in .env")

DEBUG = LOG_LEVEL == "DEBUG"

def dbg(*args):
    if DEBUG:
        print("[DBG][BOT]", *args, flush=True)

# Callback data
CB_RANDOM = "random"
CB_PICK = "pick"
CB_BACK = "back"
CB_TOPIC = "topic:"     # prefix
CB_MORE = "more:"       # prefix page
CB_CUSTOM = "custom"    # ask user to type phrase
CB_CANCEL = "cancel"    # cancel custom input

PAGE_SIZE = 8


# ---------- UI helpers ----------
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Random", callback_data=CB_RANDOM)],
        [InlineKeyboardButton("📜 Scegli frase", callback_data=CB_PICK)],
        [InlineKeyboardButton("✍️ Inserisci frase", callback_data=CB_CUSTOM)],
    ])

def pick_menu_kb(topics, page: int) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    end = min(start + PAGE_SIZE, len(topics))
    rows = []
    for i in range(start, end):
        label = topics[i]
        btn_txt = (label[:42] + "…") if len(label) > 43 else label
        rows.append([InlineKeyboardButton(btn_txt, callback_data=CB_TOPIC + str(i))])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=CB_MORE + str(page - 1)))
    if end < len(topics):
        nav.append(InlineKeyboardButton("➡️", callback_data=CB_MORE + str(page + 1)))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("🏠 Menu", callback_data=CB_BACK)])
    return InlineKeyboardMarkup(rows)

def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Annulla", callback_data=CB_CANCEL)],
        [InlineKeyboardButton("🏠 Menu", callback_data=CB_BACK)],
    ])

def pretty_intro() -> str:
    return (
        "🎧 *PodcastGen*\n"
        "_Podcast narrativo a 2 voci, generato al volo._\n\n"
        "Scegli una modalità:"
    )

def pretty_done(topic: str) -> str:
    t = (topic or "").strip()
    return f"✅ *Pronto.*\n🧩 _Spunto:_ {t}"

def short(s: str, n: int = 80) -> str:
    s = (s or "").replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")


# ---------- API ----------
def api_get_topics():
    t0 = time.time()
    r = requests.get(f"{API_URL}/topics", timeout=30)
    r.raise_for_status()
    topics = r.json()["topics"]
    dbg("api_get_topics ->", len(topics), "in", f"{(time.time()-t0):.2f}s")
    return topics

def api_generate(topic: str | None):
    payload = {
        "topic": topic,
        "language": "Italian",
        "voice_a": "Aiden",
        "voice_b": "Serena",
        "style_hint": "Ritmo radiofonico, tono caldo, pause naturali, dizione chiara.",
    }
    dbg("api_generate -> topic:", repr(topic))
    t0 = time.time()
    r = requests.post(f"{API_URL}/generate", json=payload, timeout=600)
    r.raise_for_status()
    out = r.json()
    dbg("api_generate -> done in", f"{(time.time()-t0):.2f}s",
        "script_chars:", len(out.get("script","") or ""),
        "audio_path:", out.get("audio_path"))
    return out


# ---------- Audio conversion ----------
def to_voice_ogg(input_path: str) -> str:
    base, _ = os.path.splitext(input_path)
    out_path = base + ".ogg"
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vn",
        "-c:a", "libopus",
        "-b:a", "32k",
        "-ar", "48000",
        out_path,
    ]
    dbg("ffmpeg:", " ".join(cmd))
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out_path


# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dbg("/start from:", update.effective_user.id if update.effective_user else None)
    context.user_data.pop("awaiting_phrase", None)
    await update.message.reply_text(pretty_intro(), parse_mode="Markdown", reply_markup=main_menu_kb())

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = (query.data or "").strip()

    dbg("button:", data, "from:", query.from_user.id if query.from_user else None)

    # CANCEL custom input
    if data == CB_CANCEL:
        context.user_data.pop("awaiting_phrase", None)
        await query.edit_message_text(pretty_intro(), parse_mode="Markdown", reply_markup=main_menu_kb())
        return

    # MENU
    if data == CB_BACK:
        context.user_data.pop("awaiting_phrase", None)
        await query.edit_message_text(pretty_intro(), parse_mode="Markdown", reply_markup=main_menu_kb())
        return

    # CUSTOM (ask user to type phrase)
    if data == CB_CUSTOM:
        context.user_data["awaiting_phrase"] = True
        await query.edit_message_text(
            "✍️ *Scrivi la tua frase del giorno*\n"
            "_Invia un messaggio con lo spunto. Poi genero il vocale._",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    # PICK LIST
    if data == CB_PICK:
        context.user_data.pop("awaiting_phrase", None)
        topics = context.user_data.get("topics_cache")
        if not topics:
            topics = api_get_topics()
            context.user_data["topics_cache"] = topics
        context.user_data["page"] = 0
        await query.edit_message_text(
            "📜 *Scegli una frase*",
            parse_mode="Markdown",
            reply_markup=pick_menu_kb(topics, 0),
        )
        return

    # PAGING
    if data.startswith(CB_MORE):
        context.user_data.pop("awaiting_phrase", None)
        topics = context.user_data.get("topics_cache") or api_get_topics()
        page = int(data.split("more:", 1)[1])
        context.user_data["page"] = page
        await query.edit_message_text(
            "📜 *Scegli una frase*",
            parse_mode="Markdown",
            reply_markup=pick_menu_kb(topics, page),
        )
        return

    # RANDOM
    if data == CB_RANDOM:
        context.user_data.pop("awaiting_phrase", None)
        await query.edit_message_text("⏳ *Genero (random)…*", parse_mode="Markdown")
        result = api_generate(topic=None)
        await _send_result_and_reset(query, context, result)
        return

    # TOPIC SELECT
    if data.startswith(CB_TOPIC):
        context.user_data.pop("awaiting_phrase", None)
        idx = int(data.split("topic:", 1)[1])
        topics = context.user_data.get("topics_cache") or api_get_topics()
        topic = topics[idx]
        await query.edit_message_text("⏳ *Genero…*", parse_mode="Markdown")
        result = api_generate(topic=topic)
        await _send_result_and_reset(query, context, result)
        return

    # Fallback
    context.user_data.pop("awaiting_phrase", None)
    await query.edit_message_text(pretty_intro(), parse_mode="Markdown", reply_markup=main_menu_kb())


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Riceve la frase custom quando l'utente ha premuto "Inserisci frase".
    """
    if not context.user_data.get("awaiting_phrase"):
        return  # ignoriamo testi normali: UI a pulsanti

    phrase = (update.message.text or "").strip()
    dbg("custom phrase received:", short(phrase, 140))

    context.user_data.pop("awaiting_phrase", None)

    if not phrase:
        await update.message.reply_text("⚠️ Frase vuota. Riprova dal menu.", reply_markup=main_menu_kb())
        return

    # status
    await update.message.reply_text("⏳ *Genero…*", parse_mode="Markdown")

    # generate
    result = api_generate(topic=phrase)

    # send voice + reset menu
    await _send_result_and_reset_msg(update, context, result)


async def _send_result_and_reset(query, context: ContextTypes.DEFAULT_TYPE, result: dict):
    topic = (result.get("topic") or "").strip()
    audio_path = (result.get("audio_path") or "").strip()

    await query.message.reply_text(pretty_done(topic), parse_mode="Markdown")

    await _send_voice_file(query.message, audio_path)

    # reset menu
    await query.message.reply_text(pretty_intro(), parse_mode="Markdown", reply_markup=main_menu_kb())


async def _send_result_and_reset_msg(update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict):
    topic = (result.get("topic") or "").strip()
    audio_path = (result.get("audio_path") or "").strip()

    await update.message.reply_text(pretty_done(topic), parse_mode="Markdown")

    await _send_voice_file(update.message, audio_path)

    # reset menu
    await update.message.reply_text(pretty_intro(), parse_mode="Markdown", reply_markup=main_menu_kb())


async def _send_voice_file(message, audio_path: str):
    if audio_path and os.path.exists(audio_path):
        try:
            voice_path = audio_path
            if not audio_path.lower().endswith(".ogg"):
                voice_path = to_voice_ogg(audio_path)

            dbg("sending voice:", voice_path)
            with open(voice_path, "rb") as f:
                await message.reply_voice(voice=f)

        except Exception as e:
            dbg("voice send failed:", repr(e))
            try:
                with open(audio_path, "rb") as f:
                    await message.reply_audio(audio=f)
            except Exception as e2:
                dbg("audio fallback failed:", repr(e2))
                await message.reply_text("⚠️ Errore inviando l’audio.")
    else:
        await message.reply_text("⚠️ Audio non trovato sul server.")


def main():
    dbg("ENV:", "APP_ENV=", APP_ENV, "API_URL=", API_URL, "LOG_LEVEL=", LOG_LEVEL)
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))

    # intercetta testo SOLO quando siamo in modalità awaiting_phrase
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Bot started | env=%s | api=%s", APP_ENV, API_URL)
    app.run_polling()

if __name__ == "__main__":
    main()
