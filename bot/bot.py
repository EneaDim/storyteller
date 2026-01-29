# bot/bot.py
import os
import time
import tempfile
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_URL = os.getenv("API_URL", "").strip().rstrip("/")
APP_ENV = os.getenv("APP_ENV", "dev").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper().strip()

# Normalize API_URL (allow user to provide without scheme)
if API_URL and not API_URL.startswith(("http://", "https://")):
    API_URL = "https://" + API_URL
API_URL = API_URL.rstrip("/")

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
CB_TOPIC = "topic:"     # prefix + index
CB_MORE = "more:"       # prefix + page
CB_CUSTOM = "custom"    # ask user to type a phrase
CB_CANCEL = "cancel"    # cancel custom input

PAGE_SIZE = 8


# ---------------- UI helpers ----------------
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
        "🎧 *VoiceGen*\n"
        "_Podcast narrativo a 2 voci, generato al volo._\n\n"
        "Scegli una modalità:"
    )

def pretty_done(topic: str) -> str:
    t = (topic or "").strip()
    return f"✅ *Pronto.*\n🧩 _Spunto:_ {t}"

def short(s: str, n: int = 120) -> str:
    s = (s or "").replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")


# ---------------- API calls ----------------
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
    r = requests.post(f"{API_URL}/generate", json=payload, timeout=900)
    r.raise_for_status()
    out = r.json()
    dbg(
        "api_generate -> done in", f"{(time.time()-t0):.2f}s",
        "script_chars:", len(out.get("script", "") or ""),
        "audio_url:", out.get("audio_url"),
        "audio_file:", out.get("audio_file"),
    )
    return out


# ---------------- Audio helpers ----------------
def to_voice_ogg(input_path: str) -> str:
    """
    Convert to OGG/OPUS for Telegram voice note.
    """
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

def download_audio(audio_url: str) -> str:
    """
    Download audio bytes from API and store as temp file.
    Returns local filepath.
    """
    if not audio_url:
        raise RuntimeError("audio_url missing")

    url = API_URL.rstrip("/") + audio_url
    dbg("download_audio ->", url)

    r = requests.get(url, timeout=900)
    r.raise_for_status()

    # infer suffix if possible
    suffix = ".m4a"
    if audio_url.lower().endswith(".wav"):
        suffix = ".wav"
    elif audio_url.lower().endswith(".mp3"):
        suffix = ".mp3"
    elif audio_url.lower().endswith(".ogg"):
        suffix = ".ogg"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
        f.write(r.content)
        return f.name


# ---------------- Telegram handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dbg("/start from:", update.effective_user.id if update.effective_user else None)
    context.user_data.pop("awaiting_phrase", None)
    await update.message.reply_text(pretty_intro(), parse_mode="Markdown", reply_markup=main_menu_kb())

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = (query.data or "").strip()

    dbg("button:", data, "from:", query.from_user.id if query.from_user else None)

    # Cancel custom input
    if data == CB_CANCEL:
        context.user_data.pop("awaiting_phrase", None)
        await query.edit_message_text(pretty_intro(), parse_mode="Markdown", reply_markup=main_menu_kb())
        return

    # Back to menu
    if data == CB_BACK:
        context.user_data.pop("awaiting_phrase", None)
        await query.edit_message_text(pretty_intro(), parse_mode="Markdown", reply_markup=main_menu_kb())
        return

    # Ask for custom phrase
    if data == CB_CUSTOM:
        context.user_data["awaiting_phrase"] = True
        await query.edit_message_text(
            "✍️ *Scrivi la tua frase del giorno*\n"
            "_Invia un messaggio con lo spunto (una riga). Poi genero il vocale._",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return

    # Show topics list
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

    # Paging
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

    # Random generation
    if data == CB_RANDOM:
        context.user_data.pop("awaiting_phrase", None)
        await query.edit_message_text("⏳ *Genero (random)…*", parse_mode="Markdown")
        result = api_generate(topic=None)
        await _send_result_and_reset(query.message, result)
        return

    # Topic selected
    if data.startswith(CB_TOPIC):
        context.user_data.pop("awaiting_phrase", None)
        idx = int(data.split("topic:", 1)[1])
        topics = context.user_data.get("topics_cache") or api_get_topics()
        topic = topics[idx]
        await query.edit_message_text("⏳ *Genero…*", parse_mode="Markdown")
        result = api_generate(topic=topic)
        await _send_result_and_reset(query.message, result)
        return

    # Fallback
    context.user_data.pop("awaiting_phrase", None)
    await query.edit_message_text(pretty_intro(), parse_mode="Markdown", reply_markup=main_menu_kb())

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Receives custom phrase only if user pressed 'Inserisci frase'.
    Otherwise ignore (button-first UX).
    """
    if not context.user_data.get("awaiting_phrase"):
        return

    phrase = (update.message.text or "").strip()
    dbg("custom phrase received:", short(phrase, 200))

    context.user_data.pop("awaiting_phrase", None)

    if not phrase:
        await update.message.reply_text("⚠️ Frase vuota. Riprova dal menu.", reply_markup=main_menu_kb())
        return

    await update.message.reply_text("⏳ *Genero…*", parse_mode="Markdown")
    result = api_generate(topic=phrase)
    await _send_result_and_reset(update.message, result)


async def _send_result_and_reset(message, result: dict):
    topic = (result.get("topic") or "").strip()
    audio_url = (result.get("audio_url") or "").strip()

    await message.reply_text(pretty_done(topic), parse_mode="Markdown")

    # Download + send as voice
    try:
        local_audio = download_audio(audio_url)
        dbg("downloaded ->", local_audio)

        # Send as Telegram voice note (prefer ogg)
        if local_audio.lower().endswith(".ogg"):
            voice_path = local_audio
        else:
            voice_path = to_voice_ogg(local_audio)

        dbg("sending voice ->", voice_path)
        with open(voice_path, "rb") as f:
            await message.reply_voice(voice=f)

    except Exception as e:
        dbg("send voice failed:", repr(e))
        # fallback: send as audio if we can
        try:
            if 'local_audio' in locals() and os.path.exists(local_audio):
                with open(local_audio, "rb") as f:
                    await message.reply_audio(audio=f)
            else:
                await message.reply_text("⚠️ Non riesco a recuperare/inviare l’audio.")
        except Exception as e2:
            dbg("audio fallback failed:", repr(e2))
            await message.reply_text("⚠️ Errore inviando l’audio.")

    # Reset to main menu (like /start)
    await message.reply_text(pretty_intro(), parse_mode="Markdown", reply_markup=main_menu_kb())


# ---------------- Error handler ----------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("[BOT][ERR]", repr(context.error), flush=True)


def main():
    dbg("ENV:", "APP_ENV=", APP_ENV, "API_URL=", API_URL, "LOG_LEVEL=", LOG_LEVEL)
    app = Application.builder().token(BOT_TOKEN).build()
    dbg("BOT_TOKEN sha1:", hashlib.sha1(BOT_TOKEN.encode()).hexdigest()[:8])
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("Bot started | env=%s | api=%s", APP_ENV, API_URL)
    app.run_polling()

if __name__ == "__main__":
    main()

