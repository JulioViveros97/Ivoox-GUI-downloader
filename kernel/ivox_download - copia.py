from __future__ import annotations

import logging
import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

import requests

from .episode_model import Episode
from .naming_schemes import NamingOptions, sanitize_filename_component
from .logging_utils import get_logger

try:
    from mutagen.id3 import ID3, APIC, ID3NoHeaderError
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4, MP4Cover
    MUTAGEN_AVAILABLE = True
except Exception:
    MUTAGEN_AVAILABLE = False

ID_RE = re.compile(r"_rf_(\d+)_1\.html")


def extract_audio_id_from_page_url(page_url: str) -> Optional[str]:
    m = ID_RE.search(page_url or "")
    return m.group(1) if m else None


def build_embed_url(audio_id: str) -> str:
    return f"https://www.ivoox.com/listenembeded_mn_{audio_id}_1.mp3?source=EMBEDEDHTML5"


def _guess_audio_extension(resp: requests.Response) -> str:
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    path_ext = Path(urlparse(resp.url).path).suffix.lower()
    if ctype in {"audio/mp4", "audio/x-m4a", "video/mp4"}:
        return ".m4a"
    if ctype in {"audio/mpeg", "audio/mp3"}:
        return ".mp3"
    if path_ext in {".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".opus"}:
        return path_ext
    return ".mp3"


def _guess_image_extension_from_header(head: bytes) -> str:
    """Detecta una extension de imagen a partir de bytes iniciales.

    Reemplaza el uso de imghdr, removido de la libreria estandar en
    Python 3.13. Solo cubre los formatos que el downloader necesita
    para miniaturas/portadas.
    """
    data = head or b""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def _content_disposition_filename(resp: requests.Response) -> Optional[str]:
    cd = resp.headers.get("Content-Disposition") or resp.headers.get("content-disposition")
    if not cd:
        return None
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    if not m:
        return None
    return Path(m.group(1)).name


def _natural_basename(ep: Episode, resp: requests.Response) -> str:
    cd_name = _content_disposition_filename(resp)
    ext = _guess_audio_extension(resp)
    if cd_name:
        stem = sanitize_filename_component(Path(cd_name).stem)
        if stem:
            return stem + ext

    slug = sanitize_filename_component(Path(urlparse(ep.page_url).path).stem)
    slug = re.sub(r"_rf_\d+_1$", "", slug)
    if not slug:
        slug = sanitize_filename_component(ep.title or f"episode-{ep.id}")
    return f"{ep.id}-{slug}{ext}"


def _download_thumbnail(session: requests.Session, thumbnail_url: str, audio_path: Path, logger: logging.Logger) -> Optional[Path]:
    if not thumbnail_url:
        return None
    try:
        resp = session.get(thumbnail_url, stream=True, timeout=20)
        resp.raise_for_status()
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = None
        if ctype == "image/jpeg":
            ext = ".jpg"
        elif ctype == "image/png":
            ext = ".png"
        elif ctype == "image/webp":
            ext = ".webp"
        else:
            # sniff first bytes
            head = b""
            chunks = []
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    chunks.append(chunk)
                    head += chunk
                    if len(head) >= 32:
                        break
            ext = _guess_image_extension_from_header(head)
            thumb_path = audio_path.with_suffix(ext)
            with open(thumb_path, "wb") as f:
                for c in chunks:
                    f.write(c)
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            logger.info(f"Thumbnail saved: {thumb_path.name}")
            return thumb_path

        thumb_path = audio_path.with_suffix(ext)
        with open(thumb_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        logger.info(f"Thumbnail saved: {thumb_path.name}")
        return thumb_path
    except Exception as exc:
        logger.warning(f"Could not download thumbnail: {exc}")
        return None


def _embed_cover_art(audio_path: Path, image_path: Path, logger: logging.Logger) -> bool:
    if not MUTAGEN_AVAILABLE:
        logger.warning("mutagen no está disponible; se omite embeber portada.")
        return False
    try:
        data = image_path.read_bytes()
        mime, _ = mimetypes.guess_type(str(image_path))
        mime = mime or "image/jpeg"
        suffix = audio_path.suffix.lower()
        if suffix == ".mp3":
            try:
                audio = MP3(str(audio_path), ID3=ID3)
                if audio.tags is None:
                    audio.add_tags()
            except ID3NoHeaderError:
                audio = MP3(str(audio_path), ID3=ID3)
                audio.add_tags()
            audio.tags.delall("APIC")
            audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            audio.save(v2_version=3)
            logger.info(f"Embedded cover art into MP3: {audio_path.name}")
            return True
        if suffix in {".m4a", ".mp4"}:
            audio = MP4(str(audio_path))
            image_format = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(data, imageformat=image_format)]
            audio.save()
            logger.info(f"Embedded cover art into M4A/MP4: {audio_path.name}")
            return True
        logger.warning(f"Embed cover skipped: unsupported extension {suffix}")
        return False
    except Exception as exc:
        logger.warning(f"Could not embed cover art into {audio_path.name}: {exc}")
        return False


def _maybe_apply_proposed_name(ep: Episode, current_path: Path, resp: requests.Response, opts: 'DownloadOptions', logger: logging.Logger) -> Path:
    naming = opts.naming
    if not naming or not getattr(naming, "use_proposed", False):
        return current_path
    proposed = (ep.proposed_filename or "").strip()
    if not proposed:
        return current_path
    ext = _guess_audio_extension(resp)
    stem = sanitize_filename_component(Path(proposed).stem)
    if not stem:
        return current_path
    new_path = current_path.with_name(stem + ext)
    try:
        if new_path.resolve() != current_path.resolve():
            if new_path.exists() and not opts.overwrite:
                logger.info(f"Skipping rename, target exists and overwrite=False: {new_path.name}")
                return current_path
            current_path.replace(new_path)
            logger.info(f"Renamed to proposed name: {new_path.name}")
        return new_path
    except Exception as exc:
        logger.warning(f"Could not rename to proposed name: {exc}")
        return current_path


@dataclass
class DownloadOptions:
    output_dir: Path
    naming: Optional[NamingOptions] = None
    overwrite: bool = False
    max_episodes: Optional[int] = None
    pause_seconds: float = 3.0
    embed_thumbnail: bool = False


def download_one_episode(ep: Episode, idx: int, total: int, opts: DownloadOptions, logger: logging.Logger, session: Optional[requests.Session] = None) -> None:
    if session is None:
        session = requests.Session()

    logger.info(f"[{idx}/{total}] Starting: {ep.title}")
    audio_id = (ep.id or "").strip() or extract_audio_id_from_page_url(ep.page_url or "")
    if not audio_id:
        ep.download_status = "error"
        ep.error_message = "Could not extract audio ID from episode URL"
        logger.error(ep.error_message)
        return

    embed_url = build_embed_url(audio_id)
    logger.info(f"[{audio_id}] Resolviendo audio desde embed URL")
    try:
        resp = session.get(embed_url, allow_redirects=True, stream=True, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        msg = f"Error fetching embed/CDN URL: {exc}"
        logger.error(msg)
        ep.download_status = "error"
        ep.error_message = msg
        return

    out_dir = opts.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    natural_path = out_dir / _natural_basename(ep, resp)
    logger.info(f"[{audio_id}] CDN final resuelto")

    if natural_path.exists() and not opts.overwrite:
        logger.info(f"File already exists, skipping (overwrite=False): {natural_path.name}")
        ep.download_status = "omitido"
        ep.downloaded_path = str(natural_path)
        return

    bytes_written = 0
    try:
        with open(natural_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 64):
                if not chunk:
                    continue
                f.write(chunk)
                bytes_written += len(chunk)
        logger.info(f"Escritura completada: {natural_path.name} ({bytes_written} bytes)")
    except Exception as exc:
        msg = f"Error saving audio file: {exc}"
        logger.error(msg)
        ep.download_status = "error"
        ep.error_message = msg
        return

    ep.raw_downloaded_path = str(natural_path)
    logger.info(f"Archivo bruto guardado: {natural_path.name}")

    thumb_path = None
    if ep.thumbnail_url:
        thumb_path = _download_thumbnail(session, ep.thumbnail_url, natural_path, logger)
        if thumb_path is not None:
            ep.thumbnail_path = str(thumb_path)

    final_path = _maybe_apply_proposed_name(ep, natural_path, resp, opts, logger)
    if opts.embed_thumbnail and thumb_path is not None:
        embedded_ok = _embed_cover_art(final_path, thumb_path, logger)
        if embedded_ok:
            try:
                thumb_path.unlink()
                ep.thumbnail_path = None
                logger.info(f"Temporary thumbnail deleted after embedding: {thumb_path.name}")
            except Exception as exc:
                logger.warning(f"Could not delete temporary thumbnail {thumb_path.name}: {exc}")
    
    ep.download_status = "ok"
    ep.error_message = ""
    ep.downloaded_path = str(final_path)
    logger.info(f"[{audio_id}] Descarga completada")


def download_batch(episodes: List[Episode], opts: DownloadOptions, logger: Optional[logging.Logger] = None, progress_callback=None) -> None:
    if logger is None:
        logger = get_logger("download")

    selected = [ep for ep in episodes if getattr(ep, "selected", False)]
    if opts.max_episodes is not None:
        selected = selected[:opts.max_episodes]
    total = len(selected)
    if total == 0:
        logger.info("No episodes selected for download.")
        return

    logger.info(f"Starting batch download: {total} episode(s)")
    session = requests.Session()
    try:
        for idx, ep in enumerate(selected, start=1):
            download_one_episode(ep, idx, total, opts, logger, session=session)
            if progress_callback is not None:
                try:
                    progress_callback(idx, total, ep)
                except Exception:
                    logger.exception("Error in progress callback")
            if idx < total and opts.pause_seconds > 0:
                time.sleep(opts.pause_seconds)
    finally:
        session.close()
        logger.info("Batch download finished.")
