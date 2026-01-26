import os
import logging
from logging.handlers import RotatingFileHandler

def setup_logging(app_name: str = "voice-gen") -> logging.Logger:
    env = os.getenv("APP_ENV", "dev").lower()
    default_level = "DEBUG" if env == "dev" else "INFO"
    level = os.getenv("LOG_LEVEL", default_level).upper()

    logger = logging.getLogger(app_name)
    logger.setLevel(level)
    logger.propagate = False

    # Avoid double handlers on reload
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    os.makedirs("logs", exist_ok=True)

    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(fmt)

    fh = RotatingFileHandler("logs/app.log", maxBytes=5_000_000, backupCount=3)
    fh.setLevel(level)
    fh.setFormatter(fmt)

    logger.addHandler(ch)
    logger.addHandler(fh)
    logger.info("Logging initialized (APP_ENV=%s, LOG_LEVEL=%s)", env, level)
    return logger

def dbg(logger: logging.Logger, msg: str):
    print(msg, flush=True)
    logger.debug(msg)
