from __future__ import annotations

from typing import Dict

import requests
from PySide6.QtCore import QObject, Signal, Slot


class ThumbnailLoader(QObject):
    finished = Signal(str, bytes)
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self._cache: Dict[str, bytes] = {}
        self._session = requests.Session()

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass

    @Slot(str)
    def load(self, url: str):
        if not url:
            self.failed.emit(url)
            return
        if url in self._cache:
            self.finished.emit(url, self._cache[url])
            return
        try:
            resp = self._session.get(url, timeout=20)
            resp.raise_for_status()
            data = resp.content
            self._cache[url] = data
            self.finished.emit(url, data)
        except Exception:
            self.failed.emit(url)
