from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from platformdirs import user_log_dir

from .constants import APP_NAME


def log_directory() -> Path:
    path = Path(user_log_dir(APP_NAME, appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    return path


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("steamylan")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    handler = RotatingFileHandler(
        log_directory() / "steamylan.log",
        maxBytes=1_500_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    logger.addHandler(handler)
    return logger
