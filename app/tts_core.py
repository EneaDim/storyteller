# app/tts_core.py
import os
import re
from typing import List, Tuple, Optional

import numpy as np
import torch
from huggingface_hub import snapshot_download
from qwen_tts import Qwen3TTSModel

from .logging_setup import setup_logging, dbg

logger = setup_logging("tts")

TTS_ID = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"

SPEAKER_A = "Vivian"
SPEAKER_B = "Aiden"

# OFF by default to avoid OOM on small instances (Railway)
ENABLE_CPU_QUANT = os.getenv("ENABLE_CPU_QUANT", "0") == "1"

TTS: Optional[Qwen3TTSModel] = None


def load_tts() -> Qwen3TTSModel:
    dbg(logger, "[INIT] Download/load TTS model...")
    local_path = snapshot_download(TTS_ID)

    use_cuda = torch.cuda.is_available()
    model = Qwen3TTSModel.from_pretrained(
        local_path,
        device_map="cuda" if use_cuda else "cpu",
        dtype=torch.bfloat16 if use_cuda else torch.float32,
    )

    # Optional CPU-only quantization (can OOM during quantize step)
    if (not use_cuda) and ENABLE_CPU_QUANT:
        from torch.quantization import quantize_dynamic
        import torch.nn as nn

        dbg(logger, "[INIT] Applying dynamic int8 quantization on CPU...")
        model = quantize_dynamic(model, {nn.Linear}, dtype=torch.qint8)
        dbg(logger, "[INIT] Quantization done.")

    dbg(logger, "[INIT] TTS loaded.")
    return model


def ensure_tts() -> Qwen3TTSModel:
    global TTS
    if TTS is None:
        TTS = load_tts()
    return TTS


def clean_for_tts(s: str) -> str:
    s = re.sub(r"\[[^\]]*\]", "", s, flags=re.DOTALL)
    s = re.sub(r"\([^\)]*\)", "", s, flags=re.DOTALL)
    s = s.replace("*", "")
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def parse_dialogue(text: str) -> List[Tuple[str, str]]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    pairs: List[Tuple[str, str]] = []
    for ln in lines:
        m = re.match(r"^([AB])\s*:\s*(.+)$", ln)
        if not m:
            raise ValueError(f"Invalid dialogue format line: {ln}")
        pairs.append((m.group(1), m.group(2).strip()))
    return pairs


def add_silence(sr: int, seconds: float) -> np.ndarray:
    return np.zeros(int(sr * seconds), dtype=np.float32)


def normalize_peak(x: np.ndarray, peak: float = 0.95) -> np.ndarray:
    m = float(np.max(np.abs(x))) if x.size else 0.0
    if m < 1e-9:
        return x.astype(np.float32)
    return (x * (peak / m)).astype(np.float32)


def _speaker_for_tag(tag: str) -> str:
    return SPEAKER_B if tag.upper() == "B" else SPEAKER_A


def tts_line(text: str, language: str, speaker: str, max_new_tokens: int = 64) -> Tuple[np.ndarray, int]:
    model = ensure_tts()
    with torch.inference_mode():
        wavs, sr = model.generate_custom_voice(
            text=text,
            language=language,
            speaker=speaker,
            non_streaming_mode=True,
            max_new_tokens=max_new_tokens,
        )
    return wavs[0].astype(np.float32), int(sr)

def synthesize_mono(text: str, language: str, voice: str = "A"):
    text = clean_for_tts(text)
    speaker = _speaker_for_tag(voice)
    dbg(logger, f"[TTS] mono speaker={speaker} chars={len(text)}")

    audio, sr = tts_line(text, language, speaker)
    audio = normalize_peak(audio, 0.95)
    return text, audio, sr


def synthesize_dialogue(text: str, language: str, pause_s: float = 0.18):
    text = clean_for_tts(text)
    pairs = parse_dialogue(text)

    dbg(logger, f"[TTS] dialogue lines={len(pairs)}")
    parts: List[np.ndarray] = []
    sr_ref: Optional[int] = None

    for i, (tag, line) in enumerate(pairs, start=1):
        speaker = _speaker_for_tag(tag)
        dbg(logger, f"[TTS] line {i}/{len(pairs)} tag={tag} speaker={speaker} chars={len(line)}")

        audio, sr = tts_line(line, language, speaker)
        if sr_ref is None:
            sr_ref = sr
        elif sr != sr_ref:
            raise RuntimeError(f"Sample-rate mismatch: {sr} vs {sr_ref}")

        parts.append(audio)
        parts.append(add_silence(sr_ref, pause_s))

    full = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    full = normalize_peak(full, 0.95)
    return text, full, (sr_ref or 24000)
