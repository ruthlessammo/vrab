"""Centralized logging configuration for VRAB."""

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging(
    log_path: str = "logs/vrab.log",
    level: int = logging.INFO,
) -> None:
    """Configure rotating file + stdout logging.

    Creates the log directory if it doesn't exist.
    Safe to call multiple times — idempotent.
    """
    root = logging.getLogger()
    if root.handlers:
        return  # already configured

    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")

    # stdout
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)

    # rotating file
    log_dir = os.path.dirname(log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
