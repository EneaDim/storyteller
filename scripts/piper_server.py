"""
piper_server.py
Server HTTP minimale per TTS con Piper, con 2 voci:
- female: narratrice
- male: esperto

Richiede in /models:
  <VOICE>.onnx
  <VOICE>.onnx.json
"""
import os
import subprocess
import tempfile
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel
import uvicorn

app = FastAPI()

MODELS_DIR = os.getenv("PIPER_MODELS_DIR", "/models")

# Legacy (fallback)
VOICE_F_LEGACY = os.getenv("PIPER_VOICE_F", "it_IT-paola-medium")
VOICE_M_LEGACY = os.getenv("PIPER_VOICE_M", "it_IT-riccardo-x_low")

# Per lingua (nuovo)
VOICE_F_IT = os.getenv("PIPER_VOICE_F_IT", VOICE_F_LEGACY)
VOICE_M_IT = os.getenv("PIPER_VOICE_M_IT", VOICE_M_LEGACY)
VOICE_F_EN = os.getenv("PIPER_VOICE_F_EN", "").strip()
VOICE_M_EN = os.getenv("PIPER_VOICE_M_EN", "").strip()

class TTSReq(BaseModel):
    text: str
    voice: str = "female"    # "female" | "male"
    format: str = "mp3"      # "mp3" | "wav"
    lang: str = ""           # "" | "it" | "en"
    voice_id: str = ""       # override esplicito: es "it_IT-paola-medium"
    length_scale: float = 1.0

def _normalize_lang(x: str) -> str:
    x = (x or "").strip().lower()
    if x.startswith("it"):
        return "it"
    if x.startswith("en"):
        return "en"
    return ""

def _select_voice_id(req: TTSReq) -> str:
    """
    Priorità:
    1) req.voice_id (se presente)
    2) mapping lang+gender se configurato
    3) legacy gender (PIPER_VOICE_F / PIPER_VOICE_M)
    """
    if (req.voice_id or "").strip():
        return req.voice_id.strip()

    voice_key = (req.voice or "female").strip().lower()
    if voice_key not in ("female", "male"):
        voice_key = "female"

    lang = _normalize_lang(req.lang)

    if lang == "it":
        return VOICE_M_IT if voice_key == "male" else VOICE_F_IT

    if lang == "en":
        # Se non hai configurato voci EN, fallback a legacy (tipicamente IT)
        if voice_key == "male" and VOICE_M_EN:
            return VOICE_M_EN
        if voice_key == "female" and VOICE_F_EN:
            return VOICE_F_EN
        return VOICE_M_LEGACY if voice_key == "male" else VOICE_F_LEGACY

    # lang non specificata => comportamento legacy
    return VOICE_M_LEGACY if voice_key == "male" else VOICE_F_LEGACY

def _model_path(voice_id: str) -> str:
    voice_id = (voice_id or "").strip()
    safe = "".join(c for c in voice_id if c.isalnum() or c in ("_", "-", "."))
    if not safe:
        safe = VOICE_F_LEGACY
    return os.path.join(MODELS_DIR, f"{safe}.onnx")

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.post("/tts")
def tts(req: TTSReq):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")

    voice_key = (req.voice or "female").strip().lower()
    fmt = (req.format or "mp3").strip().lower()
    length_scale = float(req.length_scale or 1.0)
    length_scale = max(0.6, min(length_scale, 1.6))
    
    if voice_key not in ("female", "male"):
        raise HTTPException(status_code=400, detail="voice must be 'female' or 'male'")
    if fmt not in ("mp3", "wav"):
        raise HTTPException(status_code=400, detail="format must be 'mp3' or 'wav'")

    voice_id = _select_voice_id(req)
    model = _model_path(voice_id)

    if not os.path.exists(model):
        raise HTTPException(status_code=500, detail=f"model file not found: {model}")
    if not os.path.exists(model + ".json"):
        raise HTTPException(status_code=500, detail=f"model config not found: {model}.json")

    with tempfile.TemporaryDirectory() as td:
        wav_path = os.path.join(td, "out.wav")
        out_path = os.path.join(td, f"out.{fmt}")

        # 1) genera WAV con Piper usando il file .onnx
        p = subprocess.run(
            ["piper", "--model", model, "--length_scale", str(length_scale), "--output_file", wav_path],
            input=text.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if p.returncode != 0:
            stderr = p.stderr.decode("utf-8", errors="ignore")
            raise HTTPException(status_code=500, detail=f"piper_failed:\n{stderr[:4000]}")

        # 2) wav -> mp3 se richiesto
        if fmt == "mp3":
            p2 = subprocess.run(
                ["ffmpeg", "-y", "-i", wav_path, "-codec:a", "libmp3lame", "-q:a", "4", out_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if p2.returncode != 0:
                stderr = p2.stderr.decode("utf-8", errors="ignore")
                raise HTTPException(status_code=500, detail=f"ffmpeg_failed:\n{stderr[:4000]}")
        else:
            out_path = wav_path

        audio = open(out_path, "rb").read()

    ctype = "audio/mpeg" if fmt == "mp3" else "audio/wav"
    return Response(content=audio, media_type=ctype)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

