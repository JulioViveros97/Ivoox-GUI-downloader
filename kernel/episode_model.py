from dataclasses import dataclass
from typing import Optional


@dataclass
class Episode:
    """Representa un episodio individual de iVoox."""

    id: str
    title: str
    page_url: str
    podcast_title: str = ""
    thumbnail_url: Optional[str] = None
    date: Optional[str] = None
    duration: Optional[str] = None

    selected: bool = True
    download_status: str = "pendiente"
    error_message: str = ""
    proposed_filename: Optional[str] = None

    raw_downloaded_path: Optional[str] = None
    downloaded_path: Optional[str] = None
    thumbnail_path: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "page_url": self.page_url,
            "podcast_title": self.podcast_title,
            "thumbnail_url": self.thumbnail_url,
            "date": self.date,
            "duration": self.duration,
            "selected": self.selected,
            "download_status": self.download_status,
            "error_message": self.error_message,
            "proposed_filename": self.proposed_filename,
            "raw_downloaded_path": self.raw_downloaded_path,
            "downloaded_path": self.downloaded_path,
            "thumbnail_path": self.thumbnail_path,
        }
