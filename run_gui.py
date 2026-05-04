# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import atexit
import ctypes
import logging
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SESSION_LOG_FILES = (
    "ivoox_launcher.log",
    "ivoox_child_boot.log",
    "ivoox_gui.log",
)

WINDOWS_APP_USER_MODEL_ID = "PedroReyes.iVooxPodcastDownloader"


def reset_session_logs(base_dir: Path) -> None:
    """Limpia los logs de la sesion actual antes de lanzar una nueva instancia.

    Tambien cierra handlers persistentes cuando se ejecuta varias veces desde
    Spyder/IPython, porque esos handlers pueden quedar vivos entre %runfile.
    """
    for logger_name in ("ivoox_launcher", "ivoox_child_boot", "ivoox_downloader", "download"):
        logger = logging.getLogger(logger_name)
        for handler in list(logger.handlers):
            try:
                handler.flush()
                handler.close()
            except Exception:
                pass
            try:
                logger.removeHandler(handler)
            except Exception:
                pass

    logs_dir = base_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    for name in SESSION_LOG_FILES:
        path = logs_dir / name
        try:
            path.write_text("", encoding="utf-8")
        except Exception:
            # No abortar el arranque solo por no poder limpiar un log.
            pass


def set_windows_app_user_model_id() -> None:
    """Fija un AppUserModelID para que Windows use el icono propio en la barra.

    Sin esto, Windows puede agrupar la ventana bajo el proceso lanzador
    anterior o mostrar iconos heredados/confusos como Spyder/Python.
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_USER_MODEL_ID)
    except Exception:
        pass

from PySide6.QtCore import QTimer, QCoreApplication, QLibraryInfo
from PySide6.QtGui import QIcon, QImageReader
from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow


def configure_qt_plugin_paths(logger: logging.Logger) -> None:
    """Refuerza rutas de plugins Qt, especialmente imageformats/JPEG.

    En modo detached/schtasks el entorno puede quedar menos poblado que desde
    Spyder/Anaconda Prompt. Esto mantiene Qt como primera ruta de decodificacion
    y deja trazas claras de formatos disponibles.
    """
    try:
        import PySide6
        pyside_dir = Path(PySide6.__file__).resolve().parent
        conda_prefix = Path(os.environ["CONDA_PREFIX"]) if os.environ.get("CONDA_PREFIX") else None
        candidates = [
            pyside_dir / "Qt" / "plugins",
            Path(QLibraryInfo.path(QLibraryInfo.PluginsPath)),
            ROOT / "PySide6" / "Qt" / "plugins",
        ]
        if conda_prefix is not None:
            candidates.extend([
                conda_prefix / "Library" / "plugins",
                conda_prefix / "Library" / "lib" / "qt6" / "plugins",
                conda_prefix / "Lib" / "site-packages" / "PySide6" / "Qt" / "plugins",
            ])
        for raw in os.environ.get("QT_PLUGIN_PATH", "").split(os.pathsep):
            if raw.strip():
                candidates.append(Path(raw.strip()))
        for candidate in candidates:
            if candidate.is_dir():
                QCoreApplication.addLibraryPath(str(candidate))
                logger.info("[QT] addLibraryPath: %s", candidate)
        logger.info("[QT] libraryPaths=%s", [str(p) for p in QCoreApplication.libraryPaths()])
        fmts = [bytes(fmt).decode("ascii", errors="ignore") for fmt in QImageReader.supportedImageFormats()]
        logger.info("[QT] supportedImageFormats=%s", fmts)
    except Exception:
        logger.error("[QT] No se pudo configurar/loguear plugins Qt:\n%s", traceback.format_exc())



def choose_thumbnail_decoder_policy(logger: logging.Logger) -> str:
    """Define politica de decodificacion de miniaturas para la GUI.

    Si el proceso detached no ve soporte JPEG/JPG en Qt, evitamos que la GUI
    intente Qt primero para JPEG y pasamos a Pillow primero. Esto no elimina
    Qt: solo cambia el orden segun el contexto real detectado.
    """
    try:
        fmts = {bytes(fmt).decode("ascii", errors="ignore").lower() for fmt in QImageReader.supportedImageFormats()}
        has_jpeg = bool({"jpg", "jpeg", "jfif"} & fmts)
        if has_jpeg:
            policy = "qt_first"
            reason = "Qt reporta soporte JPEG/JPG/JFIF"
        else:
            policy = "pillow_first"
            reason = "Qt NO reporta soporte JPEG/JPG/JFIF en este proceso"
        os.environ["IVOOX_THUMB_DECODER"] = policy
        logger.info("[THUMB] decoder_policy=%s. Motivo: %s. formats=%s", policy, reason, sorted(fmts))
        return policy
    except Exception:
        logger.error("[THUMB] No se pudo elegir politica de decoder:\n%s", traceback.format_exc())
        os.environ["IVOOX_THUMB_DECODER"] = "pillow_first"
        return "pillow_first"

from ivoox_daemon import (
    APP_MARKER_DEFAULT,
    cleanup_pidfile,
    launch_detached_app,
    setup_launcher_logger,
    show_windows_toast,
    write_pidfile,
)


def setup_child_boot_logger() -> logging.Logger:
    logs_dir = ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ivoox_child_boot")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(str(logs_dir / "ivoox_child_boot.log"), maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    logger.addHandler(fh)
    return logger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_gui.py",
        description="Entry point único para iVoox Podcast Downloader.",
    )
    parser.add_argument(
        "--ivoox-detached-child",
        action="store_true",
        help="Flag interno: esta instancia ya es la app real desacoplada.",
    )
    parser.add_argument(
        "--ivoox-marker",
        default=APP_MARKER_DEFAULT,
        help="Marker usado para detectar/gestionar el proceso detached.",
    )
    parser.add_argument(
        "--debug-direct",
        action="store_true",
        help="Modo desarrollo: no relanza detached; abre la GUI en este mismo proceso.",
    )
    parser.add_argument(
        "--start-hidden",
        action="store_true",
        help="Inicia la app real oculta en bandeja. Por defecto la GUI abre visible.",
    )
    parser.add_argument(
        "--no-toast",
        action="store_true",
        help="No mostrar toast del bootstrap launcher.",
    )
    parser.add_argument(
        "--prefer-process",
        action="store_true",
        help="Modo experimental: usar Popen directo antes que schtasks. No recomendado desde Spyder.",
    )
    return parser


def _force_window_foreground(window: MainWindow, child_log: logging.Logger) -> None:
    try:
        if hasattr(window, "show_initial_foreground"):
            window.show_initial_foreground()
        else:
            window.showNormal()
            window.show()
            window.raise_()
            window.activateWindow()
        child_log.info("[SHOW] Foreground request ejecutado.")
    except Exception:
        child_log.error("[SHOW] Error forzando primer plano:\n%s", traceback.format_exc())


def _run_real_gui(marker: str, start_hidden: bool = False) -> int:
    child_log = setup_child_boot_logger()
    launcher_log = setup_launcher_logger(ROOT)
    try:
        child_log.info("===== IVOOX GUI REAL INSTANCE BOOT =====")
        child_log.info("marker=%s", marker)
        child_log.info("start_hidden=%s", start_hidden)
        child_log.info("sys.executable=%s", sys.executable)
        child_log.info("ROOT=%s", ROOT)

        launcher_log.info("===== IVOOX GUI REAL INSTANCE =====")
        launcher_log.info(f"marker={marker}")
        launcher_log.info(f"start_hidden={start_hidden}")
        launcher_log.info(f"sys.executable={sys.executable}")
        launcher_log.info(f"ROOT={ROOT}")

        pf = write_pidfile(marker)
        child_log.info("pidfile=%s", pf)
        launcher_log.info(f"pidfile={pf}")
        atexit.register(lambda: cleanup_pidfile(marker))

        child_log.info("Configurando AppUserModelID de Windows...")
        set_windows_app_user_model_id()

        child_log.info("Configurando rutas/plugins Qt antes de QApplication...")
        configure_qt_plugin_paths(child_log)

        child_log.info("Creando QApplication...")
        app = QApplication(sys.argv[:1])
        child_log.info("Revisando rutas/plugins Qt despues de QApplication...")
        configure_qt_plugin_paths(child_log)
        choose_thumbnail_decoder_policy(child_log)
        app.setQuitOnLastWindowClosed(False)
        app.setApplicationName("iVoox Podcast Downloader")
        app.setApplicationDisplayName("iVoox Podcast Downloader")

        icon_path = ROOT / "assets" / "ivoox_downloader.ico"
        if icon_path.is_file():
            app.setWindowIcon(QIcon(str(icon_path)))
            child_log.info("Icono app cargado: %s", icon_path)
        else:
            child_log.warning("Icono app no encontrado: %s", icon_path)

        child_log.info("Creando MainWindow...")
        window = MainWindow()
        if icon_path.is_file():
            try:
                window.setWindowIcon(QIcon(str(icon_path)))
            except Exception:
                pass

        child_log.info(
            "MainWindow creada. tray_icon=%s visible=%s",
            getattr(window, "tray_icon", None) is not None,
            getattr(getattr(window, "tray_icon", None), "isVisible", lambda: False)(),
        )

        if start_hidden:
            window.hide()
            child_log.info("Ventana iniciada oculta (--start-hidden).")
            window.notify_signal.emit("iVoox Downloader activo", "La aplicación se inició oculta en la bandeja del sistema.")
        else:
            # Mostrar inmediatamente y repetirlo una vez que el event loop ya esté activo.
            _force_window_foreground(window, child_log)
            QTimer.singleShot(350, lambda: _force_window_foreground(window, child_log))
            QTimer.singleShot(1200, lambda: _force_window_foreground(window, child_log))
            child_log.info("Ventana programada para abrir visible en primer plano.")

        child_log.info("Entrando a app.exec()...")
        rc = app.exec()
        child_log.info("GUI finalizada con rc=%s", rc)
        launcher_log.info(f"GUI finalizada con rc={rc}")
        return int(rc)
    except Exception:
        child_log.error("Excepción fatal durante arranque GUI real:\n%s", traceback.format_exc())
        try:
            launcher_log.error("Excepción fatal durante arranque GUI real:\n%s", traceback.format_exc())
        except Exception:
            pass
        raise


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)

    # Limpieza de logs por sesion.
    # - Bootstrap normal: limpiar antes de lanzar el proceso real.
    # - Debug directo: limpiar antes de abrir la GUI local.
    # - Hijo detached: NO limpiar; el bootstrap ya lo hizo.
    if not args.ivoox_detached_child:
        reset_session_logs(ROOT)

    # Instancia real: la que abre GUI/tray. Tambien se usa para depurar con --debug-direct.
    if args.ivoox_detached_child or args.debug_direct:
        return _run_real_gui(marker=args.ivoox_marker, start_hidden=args.start_hidden)

    # Bootstrap: por defecto usa schtasks para que la app no dependa de Spyder/terminal.
    logger = setup_launcher_logger(ROOT)
    result = launch_detached_app(
        ROOT / "run_gui.py",
        marker=args.ivoox_marker,
        start_hidden=args.start_hidden,
        extra_args=unknown,
        logger=logger,
        prefer_schtasks=not args.prefer_process,
    )

    if result.ok:
        logger.info(f"Launcher OK: {result.message}")
        if not args.no_toast:
            show_windows_toast(
                "iVoox Downloader",
                "La app quedó ejecutándose como proceso independiente. La ventana inicial debería abrirse automáticamente.",
                on_click_open=None,
                logger=logger,
            )
        return 0

    logger.error(f"Launcher FAIL: {result.message}")
    if not args.no_toast:
        show_windows_toast(
            "Error iniciando iVoox Downloader",
            "No se pudo lanzar la app desacoplada. Revisa logs/ivoox_launcher.log.",
            on_click_open=None,
            logger=logger,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
