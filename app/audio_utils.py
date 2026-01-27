import os
import uuid
import subprocess
import numpy as np
import soundfile as sf

def wav_np_to_ogg_opus(audio: np.ndarray, sr: int, out_dir: str = "tmp") -> str:
    os.makedirs(out_dir, exist_ok=True)
    base = uuid.uuid4().hex
    wav_path = os.path.join(out_dir, f"{base}.wav")
    ogg_path = os.path.join(out_dir, f"{base}.ogg")

    sf.write(wav_path, audio.astype(np.float32), sr)

    cmd = [
        "ffmpeg", "-y",
        "-i", wav_path,
        "-vn",
        "-ac", "1",
        "-ar", "48000",
        "-c:a", "libopus",
        "-b:a", "24k",
        "-vbr", "on",
        "-application", "voip",
        ogg_path,
    ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as e:
        # keep the wav for debugging if you want; or delete it—your call
        raise RuntimeError(f"ffmpeg failed: {e.stderr[:1000]}") from e
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass

    return ogg_path

