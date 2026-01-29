import os
import re
import time
import requests
from .logging_setup import setup_logging

log = setup_logging("textgen")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b-instruct")

LINE_RE = re.compile(r"^\s*([AB])\s*[:\-–—]\s*(.+?)\s*$", re.IGNORECASE)

SYSTEM_PODCAST = (
    "Sei uno sceneggiatore di podcast narrativi moderni, scritto per essere LETTO da un TTS.\n"
    "OUTPUT: SOLO dialogo. NIENTE testo extra.\n"
    "Ogni battuta su UNA riga, e ogni riga deve iniziare con 'A:' oppure 'B:'.\n\n"
    "VINCOLI (SOFT):\n"
    "- Durata ~1 minuto.\n"
    "- 12–20 righe circa.\n"
    "- Alternanza frequente, idealmente A-B-A-B...\n"
    "- Frasi brevi, stile parlato.\n"
    "- Ultima riga: A saluta.\n\n"
    "PUNTEGGIATURA DA TTS:\n"
    "- Virgole come micro-pause spesso.\n"
    "- Punti per respirare.\n"
    "- Non terminare mai una riga con ',' '...' o '—'.\n"
    "- Evita '!'.\n\n"
    "DIVIETI:\n"
    "- Niente parentesi, niente markdown.\n"
    "- Niente titoli o elenchi.\n"
)

def _extract_dialogue_only(text: str) -> str:
    text = (text or "").strip()
    lines = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = LINE_RE.match(raw)
        if not m:
            continue
        spk = m.group(1).upper()
        content = m.group(2).strip()
        if content:
            lines.append(f"{spk}: {content}")
    return "\n".join(lines).strip()

def generate_script(topic: str, language: str = "Italian", rid: str = "-") -> str:
    user = (
        f"LINGUA: {language}\n"
        f"SPUNTO (frase del giorno): {topic}\n"
        "Scrivi ora il copione.\n"
        "Ricorda: SOLO righe A:/B:, una per riga, testo pronto per TTS.\n"
        "Se stai finendo troppo presto, continua fino a ~1 minuto.\n"
    )

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PODCAST},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {
            "temperature": 0.6,
            "num_predict": 700,
            "stop": ["\n\n\n", "```", "###", "SYSTEM:", "ASSISTANT:", "User:"],
        },
    }

    log.info("[RID %s] ollama_chat START model=%s url=%s", rid, OLLAMA_MODEL, OLLAMA_URL)
    t0 = time.time()
    r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=300)
    r.raise_for_status()
    out = (r.json().get("message", {}) or {}).get("content", "") or ""
    dt = time.time() - t0
    log.info("[RID %s] ollama_chat END elapsed=%.2fs raw_chars=%d", rid, dt, len(out))

    script = _extract_dialogue_only(out)
    lines = (script.count("\n") + 1) if script else 0
    log.info("[RID %s] script extracted lines=%d chars=%d", rid, lines, len(script or ""))

    return script
