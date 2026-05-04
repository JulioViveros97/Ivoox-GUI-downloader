from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from .episode_model import Episode

INVALID_WIN_CHARS = r'<>:"/\\|?*'


@dataclass
class NamingOptions:
    use_proposed: bool = True
    prefix: str = ""
    number_source: str = "index"  # index | from_title | from_id | none
    pad_width: int = 3
    slug_source: str = "title"    # title | none
    lowercase: bool = False
    extension: str = "mp3"


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(ch)
    )


def sanitize_filename_component(text: str) -> str:
    text = strip_accents(text or "")
    text = text.strip()
    for ch in INVALID_WIN_CHARS:
        text = text.replace(ch, " ")
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text)
    text = text.strip("-._ ")
    return text


def maybe_extract_number_from_title(title: str) -> str:
    if not title:
        return ""
    m = re.search(r"\b(\d{1,5})\b", title)
    return m.group(1) if m else ""


def remove_first_number_token(title: str) -> str:
    if not title:
        return ""
    return re.sub(r"\b\d{1,5}\b", "", title, count=1).strip(" -_:")


def build_proposed_stem(ep: Episode, ordinal: int, opts: NamingOptions) -> str:
    parts: list[str] = []

    prefix = sanitize_filename_component(opts.prefix)
    if prefix:
        parts.append(prefix)

    number = ""
    if opts.number_source == "index":
        number = str(ordinal).zfill(opts.pad_width)
    elif opts.number_source == "from_title":
        number = maybe_extract_number_from_title(ep.title)
        if number:
            number = number.zfill(opts.pad_width)
    elif opts.number_source == "from_id":
        number = (ep.id or "").zfill(opts.pad_width)

    if number:
        parts.append(number)

    slug = ""
    if opts.slug_source == "title":
        slug = sanitize_filename_component(remove_first_number_token(ep.title) or ep.title)
    if slug:
        parts.append(slug)

    if not parts:
        parts = [sanitize_filename_component(ep.title) or f"episode-{ordinal}"]

    stem = "_".join(parts)
    if opts.lowercase:
        stem = stem.lower()
    return stem


def build_proposed_filename(ep: Episode, ordinal: int, opts: NamingOptions) -> str:
    stem = build_proposed_stem(ep, ordinal, opts)
    ext = opts.extension.lstrip(".")
    return f"{stem}.{ext}" if ext else stem


def update_proposed_names(episodes: Iterable[Episode], opts: NamingOptions) -> None:
    for idx, ep in enumerate(episodes, start=1):
        ep.proposed_filename = build_proposed_filename(ep, idx, opts)
