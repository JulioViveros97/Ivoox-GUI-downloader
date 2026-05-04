from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from .episode_model import Episode

EPISODE_HREF_RE = re.compile(r"_rf_(\d+)_1\.html(?:$|\?)")
DURATION_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
ABS_DATE_DUR_RE = re.compile(r"(?P<date>\d{1,2}/\d{1,2}/\d{4})\s*[·\-]\s*(?P<dur>\d{1,2}:\d{2}(?::\d{2})?)")
REL_DATE_RE = re.compile(r"\b(?:\d+\s+(?:años?|meses?|semanas?|días?|hours?|days?|weeks?|months?|years?)|ayer|today|yesterday)\b", re.I)


@dataclass
class DiscoveryOptions:
    max_pages: int = 30
    pause_seconds: float = 1.0
    enrich_episode_pages: bool = True

    parallel_page_fetch: bool = True
    page_workers: int = 6

    parallel_episode_enrich: bool = True
    episode_workers: int = 8
    episode_pause_seconds: float = 0.0

    auto_tune_workers: bool = True
    max_workers_cap: int = 16

    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0 Safari/537.36"
    )


@dataclass
class WorkerTuning:
    cpu_logical: int
    page_workers: int
    episode_workers: int
    reason: str


def _build_session(user_agent: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent,
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    })
    return session


def _normalize_podcast_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path
    if not path.endswith('.html'):
        path = path.rstrip('/') + '_1.html'
    elif not re.search(r'_(\d+)\.html$', path):
        path = path[:-5] + '_1.html'
    parsed = parsed._replace(path=path)
    return parsed.geturl()


def _build_page_url(base_url: str, page: int) -> str:
    parsed = urlparse(base_url)
    path = re.sub(r'_(\d+)\.html$', f'_{page}.html', parsed.path)
    parsed = parsed._replace(path=path)
    return parsed.geturl()


def _extract_podcast_title(soup: BeautifulSoup) -> str:
    for selector in ["h1", "meta[property='og:title']", "title"]:
        node = soup.select_one(selector)
        if not node:
            continue
        text = node.get("content", "") if node.name == "meta" else node.get_text(" ", strip=True)
        if text:
            return text.strip()
    return ""


def _nearest_episode_container(a_tag: Tag) -> Tag:
    cur: Optional[Tag] = a_tag
    for _ in range(6):
        if cur is None:
            break
        if cur.name in {"article", "li", "section"}:
            return cur
        hrefs = [a.get("href", "") for a in cur.find_all("a", href=True)]
        rf_count = sum(1 for h in hrefs if "_rf_" in h)
        if rf_count >= 1 and len(hrefs) <= 8:
            return cur
        cur = cur.parent if isinstance(cur.parent, Tag) else None
    return a_tag.parent if isinstance(a_tag.parent, Tag) else a_tag


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.2f} s"
    minutes, sec = divmod(seconds, 60)
    return f"{int(minutes)} min {sec:.1f} s"


def _short(text: str, max_len: int = 80) -> str:
    text = _clean_text(text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"

def _clamp_int(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def recommend_initial_workers(max_cap: int = 16) -> WorkerTuning:
    """Entrega una recomendacion inicial basada solo en hardware local.

    Como el cuello de botella es principalmente red/latencia, esto se usa como
    punto de partida conservador. El ajuste fino se refina despues de medir la
    primera respuesta real de iVoox.
    """
    cpu = os.cpu_count() or 4
    cap = _clamp_int(max_cap, 1, 32)

    # Para I/O bound se permite algo mas que CPU/2, pero con techo conservador
    # para no castigar al servidor ni llenar la GUI con errores 403/429.
    page_workers = _clamp_int(round(cpu * 0.50), 3, min(8, cap))
    episode_workers = _clamp_int(round(cpu * 0.65), 4, min(10, cap))

    return WorkerTuning(
        cpu_logical=cpu,
        page_workers=page_workers,
        episode_workers=episode_workers,
        reason="hardware local: tareas I/O-bound, limites conservadores",
    )


def _recommend_workers_from_observation(
    requested_page_workers: int,
    requested_episode_workers: int,
    first_page_elapsed: float,
    first_page_bytes: int,
    max_cap: int,
) -> WorkerTuning:
    """Ajusta workers usando CPU + latencia real observada contra iVoox.

    No es un benchmark perfecto: solo mide una pagina. Pero es mejor que una
    regla fija, porque incorpora latencia, throughput aparente y capacidad local.
    Los valores de GUI se tratan como maximos permitidos.
    """
    cpu = os.cpu_count() or 4
    cap = _clamp_int(max_cap, 1, 32)
    req_page = _clamp_int(requested_page_workers, 1, cap)
    req_ep = _clamp_int(requested_episode_workers, 1, cap)

    # Base local para tareas I/O-bound.
    hw_page = _clamp_int(round(cpu * 0.50), 3, min(8, cap))
    hw_ep = _clamp_int(round(cpu * 0.70), 4, min(12, cap))

    kbps = (first_page_bytes / 1024.0 / first_page_elapsed) if first_page_elapsed > 0 else 0.0

    if first_page_elapsed < 0.75 and kbps > 120:
        factor = 1.20
        latency_label = "latencia baja"
    elif first_page_elapsed < 2.00:
        factor = 1.00
        latency_label = "latencia normal"
    elif first_page_elapsed < 4.00:
        factor = 0.75
        latency_label = "latencia media-alta"
    else:
        factor = 0.55
        latency_label = "latencia alta"

    page_workers = _clamp_int(round(hw_page * factor), 2, min(req_page, cap))
    episode_workers = _clamp_int(round(hw_ep * factor), 2, min(req_ep, cap))

    # Enriquecimiento abre muchas paginas individuales; si la primera pagina no
    # fue lenta, suele convenir darle un poco mas de concurrencia que al listado.
    if first_page_elapsed < 2.0:
        episode_workers = min(req_ep, cap, max(episode_workers, page_workers + 1))

    return WorkerTuning(
        cpu_logical=cpu,
        page_workers=page_workers,
        episode_workers=episode_workers,
        reason=(
            f"{latency_label}: primera pagina={_fmt_elapsed(first_page_elapsed)}, "
            f"throughput~{kbps:.1f} KiB/s; GUI usada como limite maximo"
        ),
    )


def _retune_workers_after_batch(current_workers: int, batch_errors: list[int], max_cap: int) -> int:
    """Reduce concurrencia si aparecen señales fuertes de bloqueo o saturacion.

    Los 404 no se consideran saturacion: normalmente solo indican que ya no hay
    mas paginas de listado. En cambio 403/429 o errores de red repetidos si
    justifican bajar workers.
    """
    if current_workers <= 2:
        return current_workers
    hard_errors = sum(1 for code in batch_errors if code in {0, 403, 429, 500, 502, 503, 504})
    if hard_errors >= max(1, current_workers // 3):
        return max(2, current_workers - 2)
    return min(current_workers, max_cap)


def _extract_title_from_container(container: Tag, href: str) -> str:
    candidates: list[str] = []

    for sel in ["h1", "h2", "h3", "h4", "strong"]:
        for node in container.find_all(sel):
            text = _clean_text(node.get_text(" ", strip=True))
            if text:
                candidates.append(text)

    for a in container.find_all("a", href=True):
        text = _clean_text(a.get_text(" ", strip=True))
        if not text:
            continue
        if a.get("href") == href:
            candidates.append(text)
        elif "_rf_" in a.get("href", ""):
            candidates.append(text)

    # Descarta textos genéricos
    filtered = [
        t for t in candidates
        if len(t) >= 5 and t.lower() not in {"ver más", "read more", "play", "escuchar"}
    ]
    if filtered:
        filtered.sort(key=lambda s: (len(s), s), reverse=True)
        return filtered[0]
    return "SIN_TITULO"


def _extract_thumb_from_container(container: Tag, title: str, podcast_title: str) -> Optional[str]:
    imgs = container.find_all("img")
    if not imgs:
        return None

    title_l = (title or "").lower()
    podcast_l = (podcast_title or "").lower()
    best_score = -10
    best_url = None

    for img in imgs:
        src = img.get("src") or img.get("data-src") or img.get("data-lazy")
        if not src:
            continue
        alt = _clean_text(img.get("alt") or img.get("title") or "").lower()
        score = 0
        if title_l and alt and title_l[:40] in alt:
            score += 10
        if title_l and alt and any(tok in alt for tok in title_l.split()[:4]):
            score += 4
        if podcast_l and alt == podcast_l:
            score -= 6
        if src.startswith("data:"):
            score -= 20
        if score > best_score:
            best_score = score
            best_url = src

    return best_url


def _extract_date_duration_from_container(container: Tag) -> tuple[Optional[str], Optional[str]]:
    text = _clean_text(container.get_text(" ", strip=True))
    duration = None
    date = None

    m = ABS_DATE_DUR_RE.search(text)
    if m:
        return m.group("date"), m.group("dur")

    m = DURATION_RE.search(text)
    if m:
        duration = m.group(0)

    m = REL_DATE_RE.search(text)
    if m:
        date = m.group(0)

    return date, duration


def _extract_episode_from_anchor(a_tag: Tag, podcast_title: str) -> Optional[Episode]:
    href = a_tag.get("href", "")
    m = EPISODE_HREF_RE.search(href)
    if not m:
        return None

    ep_id = m.group(1)
    full_url = urljoin("https://www.ivoox.com", href)
    container = _nearest_episode_container(a_tag)
    title = _extract_title_from_container(container, href)
    date_text, duration_text = _extract_date_duration_from_container(container)
    thumbnail_url = _extract_thumb_from_container(container, title, podcast_title)

    return Episode(
        id=ep_id,
        title=title,
        page_url=full_url,
        podcast_title=podcast_title,
        thumbnail_url=thumbnail_url,
        date=date_text,
        duration=duration_text,
    )


def _extract_page_episodes_from_soup(soup: BeautifulSoup, podcast_title: str) -> list[Episode]:
    page_episodes: list[Episode] = []
    local_seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not EPISODE_HREF_RE.search(href):
            continue
        ep = _extract_episode_from_anchor(a, podcast_title)
        if ep is None or not ep.id or ep.id in local_seen:
            continue
        local_seen.add(ep.id)
        page_episodes.append(ep)
    return page_episodes


def _fetch_page_worker(page: int, page_url: str, podcast_title: str, user_agent: str):
    session = _build_session(user_agent)
    t0 = time.perf_counter()
    try:
        resp = session.get(page_url, timeout=20)
        elapsed = time.perf_counter() - t0
        status_code = resp.status_code
        html_bytes = len(resp.content or b"")
        if not resp.ok:
            return page, page_url, status_code, [], None, elapsed, html_bytes

        soup = BeautifulSoup(resp.text, "html.parser")
        page_episodes = _extract_page_episodes_from_soup(soup, podcast_title)
        return page, page_url, status_code, page_episodes, None, elapsed, html_bytes
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        return page, page_url, None, [], exc, elapsed, 0
    finally:
        session.close()


def _enrich_episode_from_page(session: requests.Session, ep: Episode, logger=None) -> None:
    try:
        resp = session.get(ep.page_url, timeout=20)
        resp.raise_for_status()
    except Exception as exc:
        if logger:
            logger.warning(f"[ENRICH] No se pudo abrir {ep.page_url}: {exc}")
        return

    soup = BeautifulSoup(resp.text, "html.parser")

    h1 = soup.find("h1")
    if h1:
        title = _clean_text(h1.get_text(" ", strip=True))
        if title:
            ep.title = title

    og_title = soup.select_one("meta[property='og:title']")
    if og_title and og_title.get("content") and (not ep.title or ep.title == "SIN_TITULO"):
        ep.title = _clean_text(og_title.get("content"))

    og_img = soup.select_one("meta[property='og:image']")
    twitter_img = soup.select_one("meta[name='twitter:image']")
    for node in [og_img, twitter_img]:
        if node and node.get("content"):
            ep.thumbnail_url = node.get("content")
            break

    page_text = _clean_text(soup.get_text(" ", strip=True))
    m = ABS_DATE_DUR_RE.search(page_text)
    if m:
        ep.date = m.group("date")
        ep.duration = m.group("dur")
    else:
        if not ep.duration:
            md = DURATION_RE.search(page_text)
            if md:
                ep.duration = md.group(0)
        if not ep.date:
            mr = REL_DATE_RE.search(page_text)
            if mr:
                ep.date = mr.group(0)

    # Podcast title
    for node in soup.find_all("a", href=True):
        text = _clean_text(node.get_text(" ", strip=True))
        href = node.get("href", "")
        if text and "podcast" in href and (not ep.podcast_title or ep.podcast_title in {"", "Podcast"}):
            ep.podcast_title = text
            break


def _enrich_episode_worker(idx: int, total: int, ep: Episode, user_agent: str):
    session = _build_session(user_agent)
    t0 = time.perf_counter()
    old_title = ep.title
    old_thumb = ep.thumbnail_url
    old_date = ep.date
    old_duration = ep.duration
    try:
        _enrich_episode_from_page(session, ep, logger=None)
        elapsed = time.perf_counter() - t0
        changed = []
        if ep.title != old_title:
            changed.append("titulo")
        if ep.thumbnail_url != old_thumb:
            changed.append("thumb")
        if ep.date != old_date:
            changed.append("fecha")
        if ep.duration != old_duration:
            changed.append("duracion")
        return idx, total, ep, elapsed, None, changed
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        return idx, total, ep, elapsed, exc, []
    finally:
        session.close()


def discover_episodes(podcast_url: str, options: Optional[DiscoveryOptions] = None, logger=None) -> List[Episode]:
    if options is None:
        options = DiscoveryOptions()

    run_t0 = time.perf_counter()
    base_url = _normalize_podcast_url(podcast_url)
    requested_page_workers = max(1, int(options.page_workers))
    requested_episode_workers = max(1, int(options.episode_workers))
    max_workers = requested_page_workers
    episode_workers = requested_episode_workers

    if logger:
        logger.info(
            "[DISCOVERY] Inicio de escaneo | "
            f"max_pages={options.max_pages}, parallel={options.parallel_page_fetch}, "
            f"page_workers_max={requested_page_workers}, enrich={options.enrich_episode_pages}, "
            f"episode_workers_max={requested_episode_workers}, auto_tune={options.auto_tune_workers}, "
            f"pause_pages={options.pause_seconds:.1f}s"
        )
        logger.info(f"URL normalizada del podcast: {base_url}")

    session = _build_session(options.user_agent)
    seen_ids: set[str] = set()
    fetched_pages: list[tuple[int, list[Episode]]] = []
    podcast_title = ""
    pages_ok = 0
    pages_empty = 0
    pages_http_error = 0
    pages_network_error = 0
    listed_episode_count = 0

    try:
        page1_url = _build_page_url(base_url, 1)
        if logger:
            logger.info(f"[LIST] Página 1 -> {page1_url}")
        t0 = time.perf_counter()
        resp1 = session.get(page1_url, timeout=20)
        page1_elapsed = time.perf_counter() - t0
    except Exception as exc:
        session.close()
        if logger:
            logger.error(f"[LIST] Error de red al leer página 1: {exc}")
            logger.info(f"[DISCOVERY] Finalizado con error tras {_fmt_elapsed(time.perf_counter() - run_t0)}")
        return []

    if not resp1.ok:
        session.close()
        if logger:
            logger.warning(
                f"[LIST] Página 1 HTTP {resp1.status_code}; deteniendo búsqueda "
                f"(t={_fmt_elapsed(page1_elapsed)})."
            )
            logger.info(f"[DISCOVERY] Finalizado sin episodios tras {_fmt_elapsed(time.perf_counter() - run_t0)}")
        return []

    soup1 = BeautifulSoup(resp1.text, "html.parser")
    podcast_title = _extract_podcast_title(soup1)
    if logger and podcast_title:
        logger.info(f"Podcast: {podcast_title}")

    page1_episodes = _extract_page_episodes_from_soup(soup1, podcast_title)
    pages_ok += 1
    listed_episode_count += len(page1_episodes)
    if logger:
        logger.info(
            f"[LIST] Página 1 OK: {len(page1_episodes)} episodios, "
            f"{len(resp1.content or b'') / 1024:.1f} KiB, t={_fmt_elapsed(page1_elapsed)}. "
            f"Acumulado listado={listed_episode_count}."
        )

    if options.auto_tune_workers:
        tuning = _recommend_workers_from_observation(
            requested_page_workers=requested_page_workers,
            requested_episode_workers=requested_episode_workers,
            first_page_elapsed=page1_elapsed,
            first_page_bytes=len(resp1.content or b""),
            max_cap=options.max_workers_cap,
        )
        max_workers = tuning.page_workers
        episode_workers = tuning.episode_workers
        if logger:
            logger.info(
                f"[TUNE] Autoajuste inicial: CPU lógica={tuning.cpu_logical}, "
                f"page_workers={max_workers}/{requested_page_workers}, "
                f"episode_workers={episode_workers}/{requested_episode_workers}. Motivo: {tuning.reason}."
            )
    else:
        max_workers = _clamp_int(requested_page_workers, 1, options.max_workers_cap)
        episode_workers = _clamp_int(requested_episode_workers, 1, options.max_workers_cap)
        if logger:
            logger.info(
                f"[TUNE] Autoajuste desactivado: page_workers={max_workers}, "
                f"episode_workers={episode_workers}."
            )

    if page1_episodes:
        fetched_pages.append((1, page1_episodes))
    else:
        if logger:
            logger.info("[LIST] No se encontraron episodios en página 1. Se asume fin del listado.")
            logger.info(f"[DISCOVERY] Finalizado sin episodios tras {_fmt_elapsed(time.perf_counter() - run_t0)}")
        session.close()
        return []

    if options.max_pages > 1:
        if options.parallel_page_fetch:
            if logger:
                logger.info(
                    f"[LIST] Escaneo paralelo por páginas activado: workers={max_workers}. "
                    "Se procesará por lotes para no disparar demasiadas páginas 404."
                )

            page_results = {}
            stop_after_batch = False
            batch_start = 2
            while batch_start <= options.max_pages:
                batch_end = min(options.max_pages, batch_start + max_workers - 1)
                batch_pages = list(range(batch_start, batch_end + 1))
                batch_t0 = time.perf_counter()
                batch_found = 0
                batch_had_ok_with_episodes = False
                batch_error_codes: list[int] = []

                if logger:
                    logger.info(f"[LIST] Lanzando lote paralelo páginas {batch_start}-{batch_end} ({len(batch_pages)} tareas).")

                futures = {}
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    for page in batch_pages:
                        page_url = _build_page_url(base_url, page)
                        future = executor.submit(_fetch_page_worker, page, page_url, podcast_title, options.user_agent)
                        futures[future] = page

                    completed = 0
                    for future in as_completed(futures):
                        completed += 1
                        page, page_url, status_code, page_episodes, error, elapsed, html_bytes = future.result()
                        page_results[page] = (page_url, status_code, page_episodes, error)

                        if error is not None:
                            pages_network_error += 1
                            batch_error_codes.append(0)
                            if logger:
                                logger.error(
                                    f"[LIST] Página {page} FAIL red ({completed}/{len(batch_pages)} del lote): "
                                    f"{error} | t={_fmt_elapsed(elapsed)}"
                                )
                            continue

                        if status_code is None or status_code >= 400:
                            pages_http_error += 1
                            if status_code is not None:
                                batch_error_codes.append(int(status_code))
                            if logger:
                                logger.warning(
                                    f"[LIST] Página {page} HTTP {status_code} ({completed}/{len(batch_pages)} del lote): "
                                    f"sin episodios | t={_fmt_elapsed(elapsed)}"
                                )
                            continue

                        pages_ok += 1
                        if page_episodes:
                            batch_had_ok_with_episodes = True
                            batch_found += len(page_episodes)
                            listed_episode_count += len(page_episodes)
                            sample = _short(page_episodes[0].title or page_episodes[0].id, 70)
                            if logger:
                                logger.info(
                                    f"[LIST] Página {page} OK ({completed}/{len(batch_pages)} del lote): "
                                    f"{len(page_episodes)} episodios, {html_bytes / 1024:.1f} KiB, "
                                    f"t={_fmt_elapsed(elapsed)}. Acumulado listado={listed_episode_count}. "
                                    f"Ejemplo: {sample}"
                                )
                        else:
                            pages_empty += 1
                            if logger:
                                logger.info(
                                    f"[LIST] Página {page} OK vacía ({completed}/{len(batch_pages)} del lote): "
                                    f"0 episodios, {html_bytes / 1024:.1f} KiB, t={_fmt_elapsed(elapsed)}."
                                )

                for page in batch_pages:
                    page_url, status_code, page_episodes, error = page_results.get(page, (None, None, [], None))
                    if error is None and status_code is not None and status_code < 400 and page_episodes:
                        fetched_pages.append((page, page_episodes))

                if logger:
                    logger.info(
                        f"[LIST] Lote páginas {batch_start}-{batch_end} terminado: "
                        f"{batch_found} episodios nuevos en {_fmt_elapsed(time.perf_counter() - batch_t0)}."
                    )

                tuned_workers = _retune_workers_after_batch(max_workers, batch_error_codes, options.max_workers_cap)
                if tuned_workers != max_workers:
                    if logger:
                        logger.warning(
                            f"[TUNE] Señales de saturación/bloqueo en lote {batch_start}-{batch_end}. "
                            f"Reduciendo page_workers: {max_workers} -> {tuned_workers}."
                        )
                    max_workers = tuned_workers

                if not batch_had_ok_with_episodes:
                    stop_after_batch = True
                    if logger:
                        logger.info(
                            f"[LIST] Lote {batch_start}-{batch_end} no entregó episodios. "
                            "Se detiene el escaneo paralelo anticipadamente."
                        )
                    break

                batch_start = batch_end + 1

            if stop_after_batch and logger:
                logger.info("[LIST] Corte anticipado aplicado para evitar seguir consultando páginas vacías/404.")

        else:
            if logger:
                logger.info("[LIST] Escaneo secuencial por páginas activado.")

            for page in range(2, options.max_pages + 1):
                page_url = _build_page_url(base_url, page)
                if logger:
                    logger.info(f"[LIST] Página {page} -> {page_url}")
                try:
                    t0 = time.perf_counter()
                    resp = session.get(page_url, timeout=20)
                    elapsed = time.perf_counter() - t0
                except Exception as exc:
                    pages_network_error += 1
                    if logger:
                        logger.error(f"[LIST] Error de red al leer página {page}: {exc}")
                    break

                if not resp.ok:
                    pages_http_error += 1
                    if logger:
                        logger.warning(
                            f"[LIST] Página {page} HTTP {resp.status_code}. Deteniendo búsqueda "
                            f"(t={_fmt_elapsed(elapsed)})."
                        )
                    break

                soup = BeautifulSoup(resp.text, "html.parser")
                page_episodes = _extract_page_episodes_from_soup(soup, podcast_title)
                pages_ok += 1

                if logger:
                    logger.info(
                        f"[LIST] Página {page} OK: {len(page_episodes)} episodios, "
                        f"{len(resp.content or b'') / 1024:.1f} KiB, t={_fmt_elapsed(elapsed)}."
                    )

                if not page_episodes:
                    pages_empty += 1
                    if logger:
                        logger.info(f"[LIST] Página {page} vacía. Se asume fin del listado.")
                    break

                listed_episode_count += len(page_episodes)
                fetched_pages.append((page, page_episodes))
                if logger:
                    logger.info(f"[LIST] Acumulado listado={listed_episode_count} episodios.")

                if options.pause_seconds > 0:
                    if logger:
                        logger.info(f"[WAIT] Pausa entre páginas: {options.pause_seconds:.1f} s")
                    time.sleep(options.pause_seconds)

    order_t0 = time.perf_counter()
    final_episodes: list[Episode] = []
    if logger and fetched_pages:
        logger.info(
            f"[ORDER] Reordenando a cronología ascendente: página {fetched_pages[-1][0]} -> página {fetched_pages[0][0]}."
        )

    for page_num, page_eps in reversed(fetched_pages):
        added = 0
        for ep in reversed(page_eps):
            if ep.id in seen_ids:
                continue
            seen_ids.add(ep.id)
            final_episodes.append(ep)
            added += 1
        if logger:
            logger.info(f"[ORDER] Página {page_num}: añadidos {added}; total ordenado={len(final_episodes)}.")

    if logger:
        logger.info(f"[ORDER] Ordenamiento terminado en {_fmt_elapsed(time.perf_counter() - order_t0)}.")

    if options.enrich_episode_pages:
        enrich_t0 = time.perf_counter()
        total = len(final_episodes)

        if options.parallel_episode_enrich and total > 1:
            enrich_workers_eff = _clamp_int(episode_workers, 1, options.max_workers_cap)
            if logger:
                logger.info(
                    f"[ENRICH] Enriquecimiento paralelo activado: workers={enrich_workers_eff}, "
                    f"episodios={total}."
                )

            completed_count = 0
            errors_count = 0
            with ThreadPoolExecutor(max_workers=enrich_workers_eff) as executor:
                futures = [
                    executor.submit(_enrich_episode_worker, idx, total, ep, options.user_agent)
                    for idx, ep in enumerate(final_episodes, start=1)
                ]

                for future in as_completed(futures):
                    completed_count += 1
                    idx, total, ep, elapsed, error, changed = future.result()
                    if error is not None:
                        errors_count += 1
                        if logger:
                            logger.warning(
                                f"[ENRICH] {completed_count}/{total} completado con error | "
                                f"idx_original={idx}, id={ep.id}, t={_fmt_elapsed(elapsed)} | {error}"
                            )
                        continue

                    changed_txt = ", ".join(changed) if changed else "sin cambios mayores"
                    if logger:
                        logger.info(
                            f"[ENRICH] {completed_count}/{total} listo | idx_original={idx}, id={ep.id} | "
                            f"{changed_txt} | t={_fmt_elapsed(elapsed)} | {_short(ep.title, 90)}"
                        )

            if logger:
                elapsed = time.perf_counter() - enrich_t0
                rate = (len(final_episodes) / elapsed) if elapsed > 0 else 0.0
                logger.info(
                    f"[ENRICH] Paralelo terminado en {_fmt_elapsed(elapsed)} "
                    f"({rate:.2f} episodios/s, errores={errors_count})."
                )

        else:
            if logger:
                logger.info(f"[ENRICH] Inicio de enriquecimiento secuencial: {total} páginas de episodio.")

            for idx, ep in enumerate(final_episodes, start=1):
                one_t0 = time.perf_counter()
                old_title = ep.title
                old_thumb = ep.thumbnail_url
                old_date = ep.date
                old_duration = ep.duration

                _enrich_episode_from_page(session, ep, logger=logger)

                changed = []
                if ep.title != old_title:
                    changed.append("titulo")
                if ep.thumbnail_url != old_thumb:
                    changed.append("thumb")
                if ep.date != old_date:
                    changed.append("fecha")
                if ep.duration != old_duration:
                    changed.append("duracion")
                changed_txt = ", ".join(changed) if changed else "sin cambios mayores"

                if logger:
                    logger.info(
                        f"[ENRICH] {idx}/{total} id={ep.id} | {changed_txt} | "
                        f"t={_fmt_elapsed(time.perf_counter() - one_t0)} | {_short(ep.title, 90)}"
                    )

                if options.episode_pause_seconds > 0 and idx < total:
                    if logger:
                        logger.info(f"[WAIT] Pausa entre episodios: {options.episode_pause_seconds:.1f} s")
                    time.sleep(options.episode_pause_seconds)

            if logger:
                elapsed = time.perf_counter() - enrich_t0
                rate = (len(final_episodes) / elapsed) if elapsed > 0 else 0.0
                logger.info(f"[ENRICH] Secuencial terminado en {_fmt_elapsed(elapsed)} ({rate:.2f} episodios/s).")

    total_elapsed = time.perf_counter() - run_t0
    if logger:
        rate = (len(final_episodes) / total_elapsed) if total_elapsed > 0 else 0.0
        logger.info(
            "[DISCOVERY] Resumen: "
            f"episodios_finales={len(final_episodes)}, paginas_ok={pages_ok}, "
            f"paginas_vacias={pages_empty}, http_error={pages_http_error}, red_error={pages_network_error}, "
            f"tiempo_total={_fmt_elapsed(total_elapsed)}, rendimiento={rate:.2f} episodios/s."
        )

    session.close()
    return final_episodes
