import os
import time
import uuid
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from .logging_setup import setup_logging
from .topics import DAILY_PHRASES_DEEP, random_topic
from .textgen import generate_script
from .tts_core import synthesize_script
from .audio_utils import export_audio

load_dotenv()
log = setup_logging("api")

app = FastAPI(title="voicegen-api")

EXPORT_DIR = os.getenv("EXPORT_DIR", "tmp")
os.makedirs(EXPORT_DIR, exist_ok=True)

class GenerateRequest(BaseModel):
    topic: str | None = None
    language: str = "Italian"
    voice_a: str = "Aiden"
    voice_b: str = "Serena"
    style_hint: str = "Ritmo radiofonico, tono caldo, pause naturali, dizione chiara."

@app.get("/health")
def health():
    return {"ok": True, "app_env": os.getenv("APP_ENV", "dev")}

@app.get("/topics")
def topics():
    return {"topics": DAILY_PHRASES_DEEP}

@app.get("/audio/{filename}")
def get_audio(filename: str):
    # semplice hardening: niente path traversal
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="bad filename")
    path = os.path.join(EXPORT_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="not found")
    # Telegram per voice vuole ogg/opus, ma qui serviamo qualunque formato
    return FileResponse(path)

@app.post("/generate")
def generate(req: GenerateRequest):
    rid = uuid.uuid4().hex[:8]
    topic = (req.topic or "").strip() or random_topic()

    log.info("[RID %s] /generate START topic=%r", rid, topic)
    t0 = time.time()

    log.info("[RID %s] LLM START", rid)
    script = generate_script(topic=topic, language=req.language, rid=rid)
    log.info("[RID %s] LLM END chars=%d", rid, len(script or ""))

    log.info("[RID %s] TTS START", rid)
    sr, audio = synthesize_script(
        script=script,
        language=req.language,
        voice_a=req.voice_a,
        voice_b=req.voice_b,
        style_hint=req.style_hint,
    )
    log.info("[RID %s] TTS END", rid)

    # esporta (m4a di default)
    out_path = export_audio(sr, audio, fmt=os.getenv("EXPORT_FMT", "m4a"))
    filename = os.path.basename(out_path)

    elapsed = time.time() - t0
    log.info("[RID %s] /generate END %.2fs file=%s", rid, elapsed, filename)

    # URL relativo (il bot lo compone con API_URL)
    return {
        "topic": topic,
        "script": script,
        "audio_file": filename,
        "audio_url": f"/audio/{filename}",
    }
