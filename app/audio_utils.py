import os
import re
import wave
import datetime
import subprocess
import numpy as np
from .logging_setup import setup_logging

log = setup_logging("audio_utils")

EXPORT_DIR = os.getenv("EXPORT_DIR", "tmp")
EXPORT_FMT = os.getenv("EXPORT_FMT", "m4a")

def sanitize_for_tts(text: str) -> str:
    """
    TTS-friendly punctuation:
    - normalizza ellissi e trattini
    - evita punteggiatura 'appesa' a fine riga
    """
    t = (text or "").strip()
    t = t.replace("…", "...")
    t = t.replace("—", ", ").replace("–", "-")

    # no endings that cause infinite pauses
    t = re.sub(r"[,\-]\s*$", ".", t)
    t = re.sub(r"\.\.\.\s*$", ".", t)

    # cleanup spacing
    t = re.sub(r"\s+([,.!?])", r"\1", t)
    t = re.sub(r"([,.!?])\1+", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()

def trim_silence(wav: np.ndarray, sr: int, thresh: float = 0.004, pad_ms: int = 50) -> np.ndarray:
    x = np.asarray(wav, dtype=np.float32).flatten()
    if x.size == 0:
        return x
    a = np.abs(x)
    idx = np.where(a > thresh)[0]
    if idx.size == 0:
        return x
    pad = int(sr * pad_ms / 1000.0)
    start = max(int(idx[0]) - pad, 0)
    end = min(int(idx[-1]) + pad, x.size - 1)
    return x[start:end+1]

def peak_normalize(x: np.ndarray, target_peak: float = 0.95) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32).flatten()
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak > 1e-6:
        x = x * (target_peak / peak)
    return x

def write_wav(path: str, sr: int, audio: np.ndarray):
    audio = np.asarray(audio, dtype=np.float32)
    audio = np.clip(audio, -1.0, 1.0)
    pcm16 = (audio * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sr))
        wf.writeframes(pcm16.tobytes())

def convert_with_ffmpeg(wav_path: str, out_path: str, fmt: str):
    if fmt == "m4a":
        cmd = ["ffmpeg", "-y", "-i", wav_path, "-vn", "-c:a", "aac", "-b:a", "96k", out_path]
    elif fmt == "mp3":
        cmd = ["ffmpeg", "-y", "-i", wav_path, "-vn", "-c:a", "libmp3lame", "-b:a", "128k", out_path]
    elif fmt == "ogg":
        cmd = ["ffmpeg", "-y", "-i", wav_path, "-vn", "-c:a", "libopus", "-b:a", "48k", out_path]
    else:
        raise ValueError("fmt must be one of: m4a, mp3, ogg")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def export_audio(sr: int, audio: np.ndarray, fmt: str | None = None) -> str:
    fmt = (fmt or EXPORT_FMT).lower()
    os.makedirs(EXPORT_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"podcast_{ts}"
    wav_path = os.path.join(EXPORT_DIR, base + ".wav")
    out_path = os.path.join(EXPORT_DIR, base + f".{fmt}")

    write_wav(wav_path, sr, audio)
    convert_with_ffmpeg(wav_path, out_path, fmt=fmt)

    try:
        os.remove(wav_path)
    except Exception:
        pass

    log.info("exported: %s", out_path)
    return out_path
