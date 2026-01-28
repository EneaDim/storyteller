import re
import numpy as np
import torch
from .audio_utils import sanitize_for_tts, trim_silence, peak_normalize
from .logging_setup import setup_logging

log = setup_logging("tts_core")

from qwen_tts import Qwen3TTSModel

TTS_ID = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
LINE_RE = re.compile(r"^\s*([AB])\s*[:\-–—]\s*(.+?)\s*$", re.IGNORECASE)

_TTS = None

def load_tts():
    global _TTS
    if _TTS is not None:
        return _TTS
    use_cuda = torch.cuda.is_available()
    _TTS = Qwen3TTSModel.from_pretrained(
        TTS_ID,
        device_map="cuda" if use_cuda else "cpu",
        dtype=torch.bfloat16 if use_cuda else torch.float32,
    )
    log.info("TTS loaded | cuda=%s", use_cuda)
    return _TTS

def parse_turns(script: str):
    turns = []
    for raw in (script or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = LINE_RE.match(raw)
        if m:
            turns.append((m.group(1).upper(), m.group(2).strip()))
    return turns

def split_turns_into_4_segments(turns):
    n_segments = 4
    n = len(turns)
    if n <= 0:
        return [[] for _ in range(n_segments)]
    base = n // n_segments
    rem = n % n_segments
    sizes = [(base + (1 if i < rem else 0)) for i in range(n_segments)]
    segs, idx = [], 0
    for sz in sizes:
        segs.append(turns[idx:idx+sz])
        idx += sz
    return segs

def compress_runs(segment_turns):
    out = []
    for spk, txt in segment_turns:
        txt = (txt or "").strip()
        if not txt:
            continue
        if not out:
            out.append([spk, txt])
        else:
            if out[-1][0] == spk:
                out[-1][1] = (out[-1][1] + " " + txt).strip()
            else:
                out.append([spk, txt])
    return [(spk, txt) for spk, txt in out if txt.strip()]

def tts_one(text: str, language: str, speaker: str, style_hint: str, max_new_tokens: int = 384):
    tts = load_tts()
    text = sanitize_for_tts(text)

    gen_kwargs = dict(
        do_sample=False,
        repetition_penalty=1.05,
        subtalker_dosample=False,
        max_new_tokens=int(max_new_tokens),
    )
    wavs, sr = tts.generate_custom_voice(
        text=text,
        language=language,
        speaker=speaker,
        instruct=style_hint or "",
        non_streaming_mode=True,
        **gen_kwargs,
    )
    wav = np.asarray(wavs[0], dtype=np.float32)
    wav = trim_silence(wav, sr=int(sr))
    return wav, int(sr)

def synthesize_script(script: str, language: str, voice_a: str, voice_b: str, style_hint: str):
    turns = parse_turns(script)
    if not turns:
        raise ValueError("No turns parsed from script")

    segments = split_turns_into_4_segments(turns)

    sr_final = None
    pieces: list[np.ndarray] = []

    def add_silence(ms: int):
        nonlocal sr_final
        if sr_final is None or ms <= 0:
            return
        pieces.append(np.zeros((int(sr_final * ms / 1000.0),), dtype=np.float32))

    for seg in segments:
        runs = compress_runs(seg)
        for spk, txt in runs:
            voice = voice_a if spk == "A" else voice_b
            wav, sr = tts_one(txt, language, voice, style_hint)
            if sr_final is None:
                sr_final = sr
            pieces.append(wav)
            add_silence(90)
        add_silence(180)

    audio = np.concatenate([p.flatten() for p in pieces], axis=0)
    audio = peak_normalize(audio, 0.95)
    return sr_final, audio
