from __future__ import annotations

import logging
import mimetypes
import re
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

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

try:
    from PIL import Image, ImageOps
    PILLOW_AVAILABLE = True
except Exception:
    PILLOW_AVAILABLE = False

ID_RE = re.compile(r"_rf_(\d+)_1\.html")

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

IMAGE_HEADERS = {
    **HTTP_HEADERS,
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Referer": "https://www.ivoox.com/",
}


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


def _guess_image_mime_from_bytes(data: bytes, content_type: str = "") -> Tuple[str, str]:
    """Retorna (mime, extension) a partir de content-type y magic bytes."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in {"image/jpeg", "image/jpg"}:
        return "image/jpeg", ".jpg"
    if ctype == "image/png":
        return "image/png", ".png"
    if ctype == "image/webp":
        return "image/webp", ".webp"
    if ctype == "image/gif":
        return "image/gif", ".gif"

    ext = _guess_image_extension_from_header(data[:32])
    mime = {
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "image/jpeg")
    return mime, ext


def _normalize_thumbnail_url(thumbnail_url: str) -> str:
    url = (thumbnail_url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return urljoin("https://www.ivoox.com/", url)


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


def _download_image_bytes(session: requests.Session, thumbnail_url: str, logger: logging.Logger) -> Optional[Tuple[bytes, str, str]]:
    """Descarga bytes de portada con headers de navegador.

    Retorna (data, mime, ext) o None. Se comparte con vista previa/metadata para
    evitar que iVoox entregue respuestas distintas por falta de User-Agent/Referer.
    """
    url = _normalize_thumbnail_url(thumbnail_url)
    if not url:
        return None
    try:
        resp = session.get(url, stream=False, timeout=25, headers=IMAGE_HEADERS)
        resp.raise_for_status()
        data = resp.content or b""
        if not data:
            logger.warning("[COVER] Portada descargada vacía.")
            return None
        mime, ext = _guess_image_mime_from_bytes(data, resp.headers.get("Content-Type", ""))
        logger.info(f"[COVER] Portada descargada: {len(data)} bytes, mime={mime}, ext={ext}")
        return data, mime, ext
    except Exception as exc:
        logger.warning(f"[COVER] No se pudo descargar portada: {exc}")
        return None


def _jpeg_bytes_for_embedding(data: bytes, mime: str, logger: logging.Logger) -> Optional[bytes]:
    """Convierte la portada a JPEG compatible con APIC/MP4.

    Si ya es JPEG, retorna los bytes originales. Si es PNG/WEBP/GIF/etc., usa
    Pillow si está disponible. Esto evita embeber WEBP, que muchos reproductores
    no muestran aunque mutagen lo escriba.
    """
    if (mime or "").lower() in {"image/jpeg", "image/jpg"} and data.startswith(b"\xff\xd8\xff"):
        return data

    if not PILLOW_AVAILABLE:
        logger.warning(
            f"[COVER] La portada es {mime}; se requiere Pillow para convertir a JPEG antes de embeber."
        )
        return None

    try:
        with Image.open(BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            # Fondo blanco para imágenes con alfa.
            if img.mode in {"RGBA", "LA"} or (img.mode == "P" and "transparency" in img.info):
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.getchannel("A"))
                img = bg
            else:
                img = img.convert("RGB")
            out = BytesIO()
            img.save(out, format="JPEG", quality=92, optimize=True)
            jpg = out.getvalue()
        logger.info(f"[COVER] Portada convertida a JPEG para metadata: {len(jpg)} bytes")
        return jpg
    except Exception as exc:
        logger.warning(f"[COVER] No se pudo convertir portada a JPEG con Pillow: {exc}")
        return None


def _save_cover_temp_for_debug(audio_path: Path, cover_bytes: bytes, logger: logging.Logger) -> Optional[Path]:
    try:
        thumb_path = audio_path.with_suffix(".jpg")
        thumb_path.write_bytes(cover_bytes)
        logger.info(f"[COVER] Portada temporal guardada: {thumb_path.name}")
        return thumb_path
    except Exception as exc:
        logger.warning(f"[COVER] No se pudo guardar portada temporal: {exc}")
        return None


def _download_thumbnail(session: requests.Session, thumbnail_url: str, audio_path: Path, logger: logging.Logger) -> Optional[Path]:
    """Compatibilidad histórica: descarga portada y la deja como .jpg temporal.

    La lógica nueva usa bytes directamente para embeber; esta función queda para
    trazabilidad/compatibilidad si otra parte del proyecto usa ep.thumbnail_path.
    """
    downloaded = _download_image_bytes(session, thumbnail_url, logger)
    if downloaded is None:
        return None
    data, mime, _ext = downloaded
    jpg = _jpeg_bytes_for_embedding(data, mime, logger)
    if jpg is None:
        return None
    return _save_cover_temp_for_debug(audio_path, jpg, logger)


def _verify_embedded_cover(audio_path: Path, logger: logging.Logger) -> bool:
    """Verifica de forma básica que el archivo tenga portada escrita."""
    if not MUTAGEN_AVAILABLE:
        return False
    try:
        suffix = audio_path.suffix.lower()
        if suffix == ".mp3":
            audio = MP3(str(audio_path), ID3=ID3)
            tags = audio.tags
            ok = bool(tags and tags.getall("APIC"))
            if not ok:
                logger.warning(f"[COVER] Verificación MP3: no se encontró APIC en {audio_path.name}")
            return ok
        if suffix in {".m4a", ".mp4"}:
            audio = MP4(str(audio_path))
            ok = bool(audio.tags and audio.tags.get("covr"))
            if not ok:
                logger.warning(f"[COVER] Verificación MP4/M4A: no se encontró covr en {audio_path.name}")
            return ok
    except Exception as exc:
        logger.warning(f"[COVER] No se pudo verificar portada embebida en {audio_path.name}: {exc}")
    return False


def _embed_cover_art_bytes(audio_path: Path, image_data: bytes, mime: str, logger: logging.Logger) -> bool:
    """Embebe portada desde bytes, convirtiendo a JPEG si hace falta."""
    if not MUTAGEN_AVAILABLE:
        logger.warning("mutagen no está disponible; se omite embeber portada.")
        return False

    suffix = audio_path.suffix.lower()
    if suffix not in {".mp3", ".m4a", ".mp4"}:
        logger.warning(f"[COVER] Embed cover skipped: unsupported extension {suffix}")
        return False

    jpg = _jpeg_bytes_for_embedding(image_data, mime, logger)
    if jpg is None:
        logger.warning("[COVER] No hay bytes JPEG válidos para embeber.")
        return False

    try:
        if suffix == ".mp3":
            try:
                audio = MP3(str(audio_path), ID3=ID3)
                if audio.tags is None:
                    audio.add_tags()
            except ID3NoHeaderError:
                audio = MP3(str(audio_path), ID3=ID3)
                audio.add_tags()
            audio.tags.delall("APIC")
            audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpg))
            audio.save(v2_version=3)
            ok = _verify_embedded_cover(audio_path, logger)
            logger.info(f"[COVER] Embedded cover art into MP3: {audio_path.name}; verified={ok}")
            return ok

        if suffix in {".m4a", ".mp4"}:
            audio = MP4(str(audio_path))
            audio["covr"] = [MP4Cover(jpg, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
            ok = _verify_embedded_cover(audio_path, logger)
            logger.info(f"[COVER] Embedded cover art into M4A/MP4: {audio_path.name}; verified={ok}")
            return ok

        return False
    except Exception as exc:
        logger.warning(f"[COVER] Could not embed cover art into {audio_path.name}: {exc}")
        return False


def _embed_cover_art(audio_path: Path, image_path: Path, logger: logging.Logger) -> bool:
    """Compatibilidad histórica: embebe desde archivo temporal."""
    try:
        data = image_path.read_bytes()
        mime, _ = mimetypes.guess_type(str(image_path))
        mime = mime or _guess_image_mime_from_bytes(data)[0]
        return _embed_cover_art_bytes(audio_path, data, mime, logger)
    except Exception as exc:
        logger.warning(f"[COVER] Could not read cover file {image_path}: {exc}")
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
    update_existing_metadata: bool = True


def _try_embed_episode_cover(ep: Episode, audio_path: Path, session: requests.Session, logger: logging.Logger) -> bool:
    if not ep.thumbnail_url:
        logger.info(f"[COVER] Episodio sin thumbnail_url; no se puede embeber portada: {ep.id}")
        return False
    downloaded = _download_image_bytes(session, ep.thumbnail_url, logger)
    if downloaded is None:
        return False
    data, mime, _ext = downloaded
    ok = _embed_cover_art_bytes(audio_path, data, mime, logger)
    if ok:
        ep.thumbnail_path = None
    return ok


def download_one_episode(ep: Episode, idx: int, total: int, opts: DownloadOptions, logger: logging.Logger, session: Optional[requests.Session] = None) -> None:
    if session is None:
        session = requests.Session()
    session.headers.update(HTTP_HEADERS)

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
        resp = session.get(embed_url, allow_redirects=True, stream=True, timeout=30, headers=HTTP_HEADERS)
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
        logger.info(f"File already exists, skipping download (overwrite=False): {natural_path.name}")
        if opts.embed_thumbnail and opts.update_existing_metadata:
            logger.info(f"[COVER] Intentando actualizar metadata de archivo existente: {natural_path.name}")
            cover_ok = _try_embed_episode_cover(ep, natural_path, session, logger)
            if cover_ok:
                logger.info(f"[COVER] Metadata actualizada en archivo existente: {natural_path.name}")
            else:
                logger.warning(f"[COVER] No se pudo actualizar portada en archivo existente: {natural_path.name}")
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

    # Primero renombramos a nombre final; luego embebemos metadata sobre el archivo definitivo.
    final_path = _maybe_apply_proposed_name(ep, natural_path, resp, opts, logger)

    if opts.embed_thumbnail:
        cover_ok = _try_embed_episode_cover(ep, final_path, session, logger)
        if not cover_ok:
            logger.warning(f"[COVER] Descarga OK, pero no se logró embeber portada: {final_path.name}")

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

    logger.info(f"Starting batch download: {total} episode(s); embed_thumbnail={opts.embed_thumbnail}")
    session = requests.Session()
    session.headers.update(HTTP_HEADERS)
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
