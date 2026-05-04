from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, Signal, Slot

from kernel.ivox_discovery import DiscoveryOptions, discover_episodes
from kernel.ivox_download import DownloadOptions, download_batch
from kernel.naming_schemes import NamingOptions, update_proposed_names


class DiscoveryWorker(QObject):
    finished = Signal(list)
    error = Signal(str)

    def __init__(self, url: str, options: DiscoveryOptions, naming: NamingOptions, logger):
        super().__init__()
        self.url = url
        self.options = options
        self.naming = naming
        self.logger = logger

    @Slot()
    def run(self):
        try:
            episodes = discover_episodes(self.url, options=self.options, logger=self.logger)
            update_proposed_names(episodes, self.naming)
            self.finished.emit(episodes)
        except Exception:
            tb = traceback.format_exc()
            if self.logger:
                self.logger.error(tb)
            self.error.emit(tb)


class DownloadWorker(QObject):
    finished = Signal()
    error = Signal(str)
    episode_progress = Signal(int, int, object)

    def __init__(self, episodes, options: DownloadOptions, logger):
        super().__init__()
        self.episodes = episodes
        self.options = options
        self.logger = logger

    @Slot()
    def run(self):
        try:
            def callback(idx, total, ep):
                self.episode_progress.emit(idx, total, ep)

            download_batch(self.episodes, self.options, logger=self.logger, progress_callback=callback)
            self.finished.emit()
        except Exception:
            tb = traceback.format_exc()
            if self.logger:
                self.logger.error(tb)
            self.error.emit(tb)
