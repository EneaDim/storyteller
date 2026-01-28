import os
from fastapi import FastAPI
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

@app.post("/generate")
def generate(req: GenerateRequest):
    topic = (req.topic or "").strip() or random_topic()
    script = generate_script(topic=topic, language=req.language)

    sr, audio = synthesize_script(
        script=script,
        language=req.language,
        voice_a=req.voice_a,
        voice_b=req.voice_b,
        style_hint=req.style_hint,
    )
    path = export_audio(sr, audio, fmt=os.getenv("EXPORT_FMT", "m4a"))

    return {"topic": topic, "script": script, "audio_path": path}
