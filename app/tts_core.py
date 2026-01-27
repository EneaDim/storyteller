import re
import numpy as np
import torch
from huggingface_hub import snapshot_download
from qwen_tts import Qwen3TTSModel
from .logging_setup import setup_logging, dbg

logger = setup_logging("tts")

TTS_ID = "Qwen/Qwen3-TTS-12Hz-0.6B-VoiceDesign"
TTS = None

VOICE_A = (
    "Cinematic storytelling narrator voice.\n"
    "Deep, warm timbre with a slow, measured pace.\n"
    "Natural pauses and rich intonation.\n"
    "Audiobook or cinematic trailer style narration."
)

VOICE_B = (
    "Younger co-host voice.\n"
    "Warm but more energetic and conversational.\n"
    "Medium pace, curious tone, clear diction.\n"
    "Friendly podcast co-host style."
)

def load_tts():
    from torch.quantization import quantize_dynamic
    import torch.nn as nn

    dbg(logger, "[INIT] Download/load TTS model...")
    local_path = snapshot_download(TTS_ID)
    model = Qwen3TTSModel.from_pretrained(
        local_path,
        device_map="cuda" if torch.cuda.is_available() else "cpu",
        dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float32,
    )
    # CPU-only: dynamic int8 quantization (cuts RAM, speeds some ops)
    if not torch.cuda.is_available():
        dbg(logger, "[INIT] Applying dynamic int8 quantization on CPU...")
        try:
            model = quantize_dynamic(model, {nn.Linear}, dtype=torch.qint8)
            dbg(logger, "[INIT] Quantization done.")
        except Exception as e:
            dbg(logger, f"[INIT] Quantization skipped: {e}")

    dbg(logger, "[INIT] TTS loaded.")
    return model

def ensure_tts():
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

def parse_dialogue(text: str):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    pairs = []
    for ln in lines:
        m = re.match(r"^([AB])\s*:\s*(.+)$", ln)
        if not m:
            raise ValueError(f"Invalid dialogue format line: {ln}")
        pairs.append((m.group(1), m.group(2).strip()))
    return pairs

def add_silence(sr: int, seconds: float):
    return np.zeros(int(sr * seconds), dtype=np.float32)

def normalize_peak(x: np.ndarray, peak: float = 0.95):
    m = float(np.max(np.abs(x))) if x.size else 0.0
    if m < 1e-9:
        return x.astype(np.float32)
    return (x * (peak / m)).astype(np.float32)

def tts_line(text: str, voice_desc: str, language: str):
    model = ensure_tts()
    wavs, sr = model.generate_voice_design(
        text=text,
        language=language,
        instruct=voice_desc,
        non_streaming_mode=True,
        max_new_tokens=256,
    )
    return wavs[0].astype(np.float32), int(sr)

def synthesize_mono(text: str, language: str, voice: str = "A"):
    text = clean_for_tts(text)
    voice_desc = VOICE_A if voice.upper() == "A" else VOICE_B
    dbg(logger, f"[TTS] mono voice={voice} chars={len(text)}")
    audio, sr = tts_line(text, voice_desc, language)
    audio = normalize_peak(audio, 0.95)
    return text, audio, sr

def synthesize_dialogue(text: str, language: str, pause_s: float = 0.18):
    text = clean_for_tts(text)
    pairs = parse_dialogue(text)

    dbg(logger, f"[TTS] dialogue lines={len(pairs)}")
    parts = []
    sr_ref = None

    for i, (speaker, line) in enumerate(pairs, start=1):
        voice_desc = VOICE_A if speaker == "A" else VOICE_B
        dbg(logger, f"[TTS] line {i}/{len(pairs)} speaker={speaker} chars={len(line)}")
        audio, sr = tts_line(line, voice_desc, language)
        if sr_ref is None:
            sr_ref = sr
        elif sr != sr_ref:
            raise RuntimeError(f"Sample-rate mismatch: {sr} vs {sr_ref}")
        parts.append(audio)
        parts.append(add_silence(sr_ref, pause_s))

    full = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    full = normalize_peak(full, 0.95)
    return text, full, (sr_ref or 24000)
