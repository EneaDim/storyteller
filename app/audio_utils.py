import os
import uuid
import subprocess
import numpy as np
import soundfile as sf

def wav_np_to_ogg_opus(audio: np.ndarray, sr: int, out_dir: str = "tmp") -> str:
    """
    Convert numpy audio -> wav temp -> ogg/opus for Telegram voice messages.
    Returns ogg path.
    Requires: ffmpeg binary installed.
    """
    os.makedirs(out_dir, exist_ok=True)
    base = uuid.uuid4().hex
    wav_path = os.path.join(out_dir, f"{base}.wav")
    ogg_path = os.path.join(out_dir, f"{base}.ogg")

    sf.write(wav_path, audio.astype(np.float32), sr)

    cmd = [
        "ffmpeg", "-y",
        "-i", wav_path,
        "-ac", "1",
        "-ar", "48000",
        "-c:a", "libopus",
        "-b:a", "24k",
        "-vbr", "on",
        "-application", "voip",
        ogg_path
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        os.remove(wav_path)
    except OSError:
        pass

    return ogg_path
