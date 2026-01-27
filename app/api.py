import os
import time
from fastapi import FastAPI, Body, HTTPException
from fastapi.responses import FileResponse
from .logging_setup import setup_logging, dbg
from .tts_core import synthesize_mono, synthesize_dialogue
from .audio_utils import wav_np_to_ogg_opus

logger = setup_logging("api")
app = FastAPI(title="Qwen3-TTS Voice API")

TMP_DIR = "tmp"
os.makedirs(TMP_DIR, exist_ok=True)

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/voice/mono")
def voice_mono(
    text: str = Body(..., embed=True),
    language: str = Body("Italian", embed=True),
    speaker: str = Body("A", embed=True),  # A/B tag
):
    try:
        t0 = time.time()
        speaker = (speaker or "A").upper()
        dbg(logger, f"[REQ] /voice/mono speaker={speaker} lang={language} chars={len(text)}")

        _, audio, sr = synthesize_mono(text, language=language, voice=speaker)
        ogg_path = wav_np_to_ogg_opus(audio, sr, out_dir=TMP_DIR)

        dbg(logger, f"[OK] /voice/mono in {time.time()-t0:.2f}s -> {ogg_path}")
        return FileResponse(ogg_path, media_type="audio/ogg", filename="voice.ogg")
    except Exception as e:
        logger.exception("mono failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/voice/dialogue")
def voice_dialogue(
    text: str = Body(..., embed=True),
    language: str = Body("Italian", embed=True),
    pause_s: float = Body(0.18, embed=True),
):
    try:
        t0 = time.time()
        dbg(logger, f"[REQ] /voice/dialogue lang={language} chars={len(text)} pause={pause_s}")

        _, audio, sr = synthesize_dialogue(text, language=language, pause_s=float(pause_s))
        ogg_path = wav_np_to_ogg_opus(audio, sr, out_dir=TMP_DIR)

        dbg(logger, f"[OK] /voice/dialogue in {time.time()-t0:.2f}s -> {ogg_path}")
        return FileResponse(ogg_path, media_type="audio/ogg", filename="voice.ogg")
    except Exception as e:
        logger.exception("dialogue failed")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app.api:app", host="0.0.0.0", port=port, reload=True)
