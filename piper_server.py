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
VOICE_F = os.getenv("PIPER_VOICE_F", "it_IT-paola-medium")
VOICE_M = os.getenv("PIPER_VOICE_M", "it_IT-riccardo-x_low")

class TTSReq(BaseModel):
    text: str
    voice: str = "female"   # "female" | "male"
    format: str = "mp3"     # "mp3" | "wav"

def _model_path(voice_key: str) -> str:
    voice_key = (voice_key or "female").strip().lower()
    if voice_key == "male":
        base = VOICE_M
    else:
        base = VOICE_F
    return os.path.join(MODELS_DIR, f"{base}.onnx")

@app.post("/tts")
def tts(req: TTSReq):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")

    voice_key = (req.voice or "female").strip().lower()
    fmt = (req.format or "mp3").strip().lower()
    if voice_key not in ("female", "male"):
        raise HTTPException(status_code=400, detail="voice must be 'female' or 'male'")
    if fmt not in ("mp3", "wav"):
        raise HTTPException(status_code=400, detail="format must be 'mp3' or 'wav'")

    model = _model_path(voice_key)
    if not os.path.exists(model):
        raise HTTPException(status_code=500, detail=f"model file not found: {model}")
    if not os.path.exists(model + ".json"):
        raise HTTPException(status_code=500, detail=f"model config not found: {model}.json")

    with tempfile.TemporaryDirectory() as td:
        wav_path = os.path.join(td, "out.wav")
        out_path = os.path.join(td, f"out.{fmt}")

        # 1) genera WAV con Piper usando il file .onnx
        p = subprocess.run(
            ["piper", "--model", model, "--output_file", wav_path],
            input=text.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if p.returncode != 0:
            stderr = p.stderr.decode("utf-8", errors="ignore")
            # ritorniamo più dettaglio possibile
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

    # Response raw bytes
    ctype = "audio/mpeg" if fmt == "mp3" else "audio/wav"
    return Response(content=audio, media_type=ctype)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

