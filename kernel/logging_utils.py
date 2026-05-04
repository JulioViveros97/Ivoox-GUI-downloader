from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional


class QtLogHandler(logging.Handler):
    def __init__(self, callback: Callable[[str], None]):
        super().__init__()
        self.callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.callback(self.format(record))
        except Exception:
            self.handleError(record)


def get_logger(
    name: str = "ivoox_downloader",
    qt_callback: Optional[Callable[[str], None]] = None,
    log_dir: str | Path = "logs",
    log_filename: str = "ivoox_gui.log",
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    file_path = log_path / log_filename

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    console_formatter = logging.Formatter("[%(levelname)s] %(message)s")

    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in logger.handlers):
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(console_formatter)
        logger.addHandler(ch)

    if not any(isinstance(h, logging.FileHandler) and Path(getattr(h, 'baseFilename', '')) == file_path.resolve() for h in logger.handlers):
        fh = logging.FileHandler(file_path, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    if qt_callback is not None:
        existing_qt = None
        for h in logger.handlers:
            if isinstance(h, QtLogHandler):
                existing_qt = h
                break
        if existing_qt is None:
            qh = QtLogHandler(qt_callback)
            qh.setLevel(logging.INFO)
            qh.setFormatter(formatter)
            logger.addHandler(qh)
        else:
            existing_qt.callback = qt_callback

    return logger
