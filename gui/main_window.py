# -*- coding: utf-8 -*-
import os
import threading
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

from PySide6.QtCore import Qt, Signal, Slot, QUrl
from PySide6.QtGui import QPixmap, QIcon, QAction, QDesktopServices, QImage
from PySide6.QtWidgets import (
    QMainWindow, QSplitter, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QDoubleSpinBox, QFileDialog,
    QPlainTextEdit, QCheckBox, QComboBox, QGroupBox, QWidget, QSizePolicy,
    QProgressBar, QApplication, QMenu, QMessageBox, QSystemTrayIcon, QStyle
)
from kernel.logging_utils import get_logger
from kernel.ivox_discovery import discover_episodes, DiscoveryOptions, recommend_initial_workers
from kernel.ivox_download import download_batch, DownloadOptions
from kernel.naming_schemes import NamingOptions, update_proposed_names
from kernel.episode_model import Episode
from gui.collapsible_box import CollapsibleBox
from gui.episodes_table import EpisodesTable


class MainWindow(QMainWindow):
    log_signal = Signal(str)
    thumbnail_signal = Signal(int, bytes)
    episodes_loaded = Signal(list)
    busy_state_changed = Signal(bool)
    refresh_status_signal = Signal()
    download_progress_signal = Signal(int, int)
    notify_signal = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("iVoox Podcast Downloader")
        self.resize(1500, 850)

        self._allow_real_close = False
        self._is_busy = False
        self.tray_icon = None
        self.app_icon = self._load_app_icon()
        if not self.app_icon.isNull():
            self.setWindowIcon(self.app_icon)

        self.logger = get_logger(qt_callback=self._log_from_threads)
        self.episodes: list[Episode] = []
        self._thumbnail_request_id = 0

        splitter = QSplitter(Qt.Horizontal, self)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_layout.setSpacing(6)

        box_source = CollapsibleBox("Fuente del podcast", parent=self)
        box_source.contentLayout().addLayout(self._build_source_layout())
        left_layout.addWidget(box_source)

        box_eps = CollapsibleBox("Episodios detectados", parent=self)
        eps_layout = QVBoxLayout()
        eps_layout.setContentsMargins(4, 4, 4, 4)
        eps_layout.setSpacing(4)
        self.table = EpisodesTable(self)
        eps_layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        btn_sel_all = QPushButton("Seleccionar todo")
        btn_desel_all = QPushButton("Deseleccionar todo")
        btn_rm = QPushButton("Eliminar fila(s)")
        btn_up = QPushButton("Subir")
        btn_down = QPushButton("Bajar")
        btn_sel_all.clicked.connect(self.table.select_all)
        btn_desel_all.clicked.connect(self.table.deselect_all)
        btn_rm.clicked.connect(self.table.remove_selected_rows)
        btn_up.clicked.connect(self.table.move_selected_up)
        btn_down.clicked.connect(self.table.move_selected_down)
        btn_row.addWidget(btn_sel_all)
        btn_row.addWidget(btn_desel_all)
        btn_row.addWidget(btn_rm)
        btn_row.addWidget(btn_up)
        btn_row.addWidget(btn_down)
        btn_row.addStretch()
        eps_layout.addLayout(btn_row)
        box_eps.setContentLayout(eps_layout)
        left_layout.addWidget(box_eps)

        box_export = CollapsibleBox("Exportación / Descarga", parent=self)
        box_export.contentLayout().addLayout(self._build_export_layout())
        left_layout.addWidget(box_export)
        left_layout.addStretch()

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 4, 4, 4)
        right_layout.setSpacing(6)

        thumb_group = QGroupBox("Miniatura del episodio seleccionado")
        thumb_layout = QVBoxLayout(thumb_group)
        self.thumbnail_label = QLabel("Sin selección")
        self.thumbnail_label.setAlignment(Qt.AlignCenter)
        self.thumbnail_label.setMinimumHeight(240)
        self.thumbnail_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        thumb_layout.addWidget(self.thumbnail_label)
        right_layout.addWidget(thumb_group)

        log_group = QGroupBox("Log")
        log_layout = QVBoxLayout(log_group)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        log_layout.addWidget(self.log_edit)
        right_layout.addWidget(log_group)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.setCentralWidget(splitter)

        self.log_signal.connect(self._append_log)
        self.thumbnail_signal.connect(self._set_thumbnail_from_bytes)
        self.table.itemSelectionChanged.connect(self._on_episode_selection_changed)
        self.table.cellClicked.connect(self._on_episode_cell_clicked)
        self.table.currentCellChanged.connect(self._on_episode_current_cell_changed)
        self.episodes_loaded.connect(self._on_episodes_loaded)
        self.busy_state_changed.connect(self._update_ui_state_busy)
        self.refresh_status_signal.connect(self._on_refresh_status)
        self.download_progress_signal.connect(self._on_download_progress)
        self.notify_signal.connect(self._show_tray_message)
        self._apply_initial_worker_recommendation()
        self._setup_tray_icon()
        self._update_ui_state_busy(False)
        self.logger.info("[GUI] MainWindow inicializada correctamente.")


    def _project_root(self) -> Path:
        return Path(__file__).resolve().parents[1]

    def _load_app_icon(self) -> QIcon:
        """Carga el icono propio de la app, con fallback al icono estandar."""
        candidates = [
            self._project_root() / "assets" / "ivoox_downloader.ico",
            self._project_root() / "assets" / "ivoox_downloader.png",
        ]
        for path in candidates:
            try:
                if path.is_file():
                    icon = QIcon(str(path))
                    if not icon.isNull():
                        return icon
            except Exception:
                pass
        try:
            return self.style().standardIcon(QStyle.SP_MediaPlay)
        except Exception:
            return QIcon()

    def _setup_tray_icon(self) -> None:
        """Crea el icono de bandeja y su menu contextual."""
        try:
            available = QSystemTrayIcon.isSystemTrayAvailable()
        except Exception as exc:
            self.logger.warning(f"[TRAY] No se pudo consultar disponibilidad de system tray: {exc}")
            available = False

        self.logger.info(f"[TRAY] System tray disponible: {available}")
        if not available:
            self.tray_icon = None
            self.logger.warning("[TRAY] No se creara icono de bandeja. La X cerrara la ventana normalmente.")
            return

        icon = self.app_icon if not self.app_icon.isNull() else self.style().standardIcon(QStyle.SP_ComputerIcon)
        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setToolTip("iVoox Podcast Downloader")

        menu = QMenu(self)
        act_show = QAction("Mostrar ventana", self)
        act_hide = QAction("Ocultar ventana", self)
        act_open_out = QAction("Abrir carpeta de salida", self)
        act_copy_log = QAction("Copiar log", self)
        act_quit = QAction("Salir definitivamente", self)

        act_show.triggered.connect(self._tray_show_window)
        act_hide.triggered.connect(self.hide)
        act_open_out.triggered.connect(self._tray_open_output_dir)
        act_copy_log.triggered.connect(self._tray_copy_log)
        act_quit.triggered.connect(self._tray_quit_application)

        menu.addAction(act_show)
        menu.addAction(act_hide)
        menu.addSeparator()
        menu.addAction(act_open_out)
        menu.addAction(act_copy_log)
        menu.addSeparator()
        menu.addAction(act_quit)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()
        self.logger.info(f"[TRAY] Icono inicializado. visible={self.tray_icon.isVisible()}, icon_null={icon.isNull()}")

    def _tray_show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.logger.info("[TRAY] Ventana restaurada desde bandeja.")

    def _tray_open_output_dir(self) -> None:
        path_txt = self.edit_output_dir.text().strip() if hasattr(self, "edit_output_dir") else ""
        path = Path(path_txt) if path_txt else self._project_root()
        if not path.exists():
            self.logger.warning(f"[TRAY] Carpeta no existe: {path}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def _tray_copy_log(self) -> None:
        QApplication.clipboard().setText(self.log_edit.toPlainText())
        self._show_tray_message("Log copiado", "El log fue copiado al portapapeles.")

    def _tray_quit_application(self) -> None:
        if self._is_busy:
            answer = QMessageBox.question(
                self,
                "Salir con tarea activa",
                "Hay una tarea en curso. Si sales ahora, la operación se interrumpirá.\n\n¿Deseas salir de todas formas?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self.logger.info("[TRAY] Salida definitiva solicitada por el usuario.")
        self._allow_real_close = True
        if self.tray_icon is not None:
            self.tray_icon.hide()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.isVisible() and not self.isMinimized():
                self.hide()
                self.logger.info("[TRAY] Ventana ocultada desde icono de bandeja.")
            else:
                self._tray_show_window()

    @Slot(str, str)
    def _show_tray_message(self, title: str, message: str) -> None:
        self.logger.info(f"[NOTIFY] {title}: {message}")
        if self.tray_icon is not None and self.tray_icon.isVisible():
            try:
                self.tray_icon.showMessage(title, message, QSystemTrayIcon.Information, 7000)
            except Exception as exc:
                self.logger.warning(f"[TRAY] No se pudo mostrar notificación: {exc}")

    def closeEvent(self, event):
        if self._allow_real_close or self.tray_icon is None:
            self.logger.info("[GUI] closeEvent aceptado; la app se cerrará.")
            event.accept()
            return
        event.ignore()
        self.hide()
        msg = "La aplicación sigue ejecutándose en segundo plano. Usa el icono de bandeja para restaurarla o salir definitivamente."
        self.logger.info("[GUI] closeEvent interceptado: ventana oculta en bandeja.")
        self._show_tray_message("iVoox Downloader sigue activo", msg)


    def show_initial_foreground(self) -> None:
        """Muestra la ventana inicial en primer plano y deja el tray activo.

        Se llama desde run_gui.py en la instancia real detached. No se usa
        cuando la app se inicia explicitamente con --start-hidden.
        """
        self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        self.logger.info("[GUI] Ventana inicial mostrada en primer plano.")

    def _build_source_layout(self):
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        row_url = QHBoxLayout()
        row_url.addWidget(QLabel("URL podcast:"))
        self.edit_podcast_url = QLineEdit()
        self.edit_podcast_url.setPlaceholderText("URL de la página de episodios (p.ej. https://www.ivoox.com/..._1.html)")
        row_url.addWidget(self.edit_podcast_url, 1)

        row_params = QHBoxLayout()
        row_params.addWidget(QLabel("Máx. páginas:"))
        self.spin_max_pages = QSpinBox()
        self.spin_max_pages.setRange(1, 200)
        self.spin_max_pages.setValue(30)
        row_params.addWidget(self.spin_max_pages)
        row_params.addSpacing(12)
        row_params.addWidget(QLabel("Pausa entre páginas [s]:"))
        self.spin_pause_pages = QDoubleSpinBox()
        self.spin_pause_pages.setRange(0.0, 60.0)
        self.spin_pause_pages.setDecimals(1)
        self.spin_pause_pages.setValue(1.0)
        row_params.addWidget(self.spin_pause_pages)
        row_params.addSpacing(12)
        row_params.addWidget(QLabel("Workers páginas máx.:"))
        self.spin_page_workers = QSpinBox()
        self.spin_page_workers.setRange(1, 32)
        self.spin_page_workers.setValue(6)
        self.spin_page_workers.setToolTip("Máximo de workers para leer páginas del listado. Si Autoajustar está activo, el kernel puede reducir este valor según latencia/errores.")
        row_params.addWidget(self.spin_page_workers)
        row_params.addSpacing(12)
        row_params.addWidget(QLabel("Workers enrich máx.:"))
        self.spin_episode_workers = QSpinBox()
        self.spin_episode_workers.setRange(1, 32)
        self.spin_episode_workers.setValue(8)
        self.spin_episode_workers.setToolTip("Máximo de workers para enriquecer páginas individuales de episodios. Es el cuello de botella usual en podcasts grandes.")
        row_params.addWidget(self.spin_episode_workers)
        row_params.addStretch()

        row_checks = QHBoxLayout()
        self.check_parallel_scan = QCheckBox("Escaneo paralelo por páginas")
        self.check_parallel_scan.setChecked(True)
        self.check_parallel_enrich = QCheckBox("Enriquecimiento paralelo")
        self.check_parallel_enrich.setChecked(True)
        self.check_auto_workers = QCheckBox("Autoajustar workers según equipo/latencia")
        self.check_auto_workers.setChecked(True)
        row_checks.addWidget(self.check_parallel_scan)
        row_checks.addWidget(self.check_parallel_enrich)
        row_checks.addWidget(self.check_auto_workers)
        row_checks.addStretch()

        self.btn_scan = QPushButton("Escanear podcast")
        self.btn_scan.clicked.connect(self._on_scan_clicked)

        root.addLayout(row_url)
        root.addLayout(row_params)
        root.addLayout(row_checks)
        root.addWidget(self.btn_scan)
        return root

    def _build_export_layout(self):
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        row_out = QHBoxLayout()
        row_out.addWidget(QLabel("Carpeta de salida:"))
        self.edit_output_dir = QLineEdit()
        self.btn_browse = QPushButton("Examinar...")
        self.btn_browse.clicked.connect(self._on_browse_output)
        row_out.addWidget(self.edit_output_dir, 1)
        row_out.addWidget(self.btn_browse)

        self.check_use_proposed = QCheckBox("Usar nombres propuestos")
        self.check_use_proposed.setChecked(True)

        row_naming = QHBoxLayout()
        row_naming.addWidget(QLabel("Prefijo:"))
        self.edit_prefix = QLineEdit()
        self.edit_prefix.setPlaceholderText("Prefijo")
        self.edit_prefix.setMinimumWidth(140)
        row_naming.addWidget(self.edit_prefix)
        row_naming.addWidget(QLabel("Fuente del número:"))
        self.combo_number_source = QComboBox()
        self.combo_number_source.addItems(["index", "from_title", "from_id"])
        self.combo_number_source.setCurrentText("index")
        row_naming.addWidget(self.combo_number_source)
        row_naming.addWidget(QLabel("Relleno (dígitos):"))
        self.spin_pad_width = QSpinBox()
        self.spin_pad_width.setRange(1, 6)
        self.spin_pad_width.setValue(3)
        row_naming.addWidget(self.spin_pad_width)
        row_naming.addWidget(QLabel("Fuente de slug:"))
        self.combo_slug_source = QComboBox()
        self.combo_slug_source.addItems(["title", "none"])
        self.combo_slug_source.setCurrentText("title")
        row_naming.addWidget(self.combo_slug_source)
        row_naming.addStretch()

        row_flags = QHBoxLayout()
        self.check_lowercase = QCheckBox("Nombre en minúsculas")
        self.check_lowercase.setChecked(True)
        self.check_overwrite = QCheckBox("Sobrescribir archivos existentes")
        self.check_overwrite.setChecked(False)
        self.check_embed_thumbnail = QCheckBox("Embeber portada JPG en metadatos")
        self.check_embed_thumbnail.setChecked(True)
        row_flags.addWidget(self.check_lowercase)
        row_flags.addWidget(self.check_overwrite)
        row_flags.addWidget(self.check_embed_thumbnail)
        row_flags.addSpacing(12)
        row_flags.addWidget(QLabel("Pausa entre descargas [s]:"))
        self.spin_pause_download = QDoubleSpinBox()
        self.spin_pause_download.setRange(0.0, 120.0)
        self.spin_pause_download.setDecimals(1)
        self.spin_pause_download.setValue(3.0)
        row_flags.addWidget(self.spin_pause_download)
        row_flags.addStretch()
        
        self.btn_refresh_names = QPushButton("Refrescar nombres")
        self.btn_refresh_names.clicked.connect(self._on_refresh_names)
        self.btn_download = QPushButton("Descargar episodios seleccionados")
        self.btn_download.clicked.connect(self._on_download_clicked)
        
        self.download_progress = QProgressBar()
        self.download_progress.setRange(0, 100)
        self.download_progress.setValue(0)
        self.download_progress.setTextVisible(True)
        self.download_progress.setFormat("%p%")
        self.download_progress.setStyleSheet(
            "QProgressBar { border: 1px solid #999; border-radius: 3px; text-align: center; }"
            "QProgressBar::chunk { background-color: #2ea043; }"
        )
        
        root.addLayout(row_out)
        root.addWidget(self.check_use_proposed)
        root.addLayout(row_naming)
        root.addLayout(row_flags)
        root.addWidget(self.btn_refresh_names)
        root.addWidget(self.btn_download)
        root.addWidget(self.download_progress)
        return root



    @Slot(str)
    def _log_from_threads(self, text: str):
        self.log_signal.emit(text)

    @Slot(str)
    def _append_log(self, text: str):
        self.log_edit.appendPlainText(text)
        self.log_edit.verticalScrollBar().setValue(self.log_edit.verticalScrollBar().maximum())

    @Slot(bool)
    def _update_ui_state_busy(self, busy: bool):
        self._is_busy = bool(busy)
        widgets = [
            self.edit_podcast_url,
            self.spin_max_pages,
            self.spin_pause_pages,
            self.spin_page_workers,
            self.spin_episode_workers,
            self.check_parallel_scan,
            self.check_parallel_enrich,
            self.check_auto_workers,
            self.edit_output_dir,
            self.btn_browse,
            self.check_use_proposed,
            self.edit_prefix,
            self.combo_number_source,
            self.spin_pad_width,
            self.combo_slug_source,
            self.check_lowercase,
            self.check_overwrite,
            self.check_embed_thumbnail,
            self.spin_pause_download,
            self.btn_scan,
            self.btn_refresh_names,
            self.btn_download,
        ]
        for w in widgets:
            w.setEnabled(not busy)
        
        if not busy:
            self.download_progress.setRange(0, 100)
            self.download_progress.setValue(0)
            self.download_progress.setFormat("%p%")

    def _apply_initial_worker_recommendation(self):
        try:
            tuning = recommend_initial_workers(max_cap=16)
            self.spin_page_workers.setValue(tuning.page_workers)
            self.spin_episode_workers.setValue(tuning.episode_workers)
            self.logger.info(
                f"[SYSTEM] CPU lógica detectada: {tuning.cpu_logical}. "
                f"Workers sugeridos al iniciar: pages={tuning.page_workers}, "
                f"enrich={tuning.episode_workers}. Motivo: {tuning.reason}."
            )
            self.logger.info(
                "[SYSTEM] Autoajuste activo: esos valores son máximos; durante el escaneo "
                "se refinan con la latencia real de iVoox y señales de error HTTP."
            )
        except Exception:
            self.logger.exception("No se pudo calcular recomendación inicial de workers.")


    def _on_browse_output(self):
        path = QFileDialog.getExistingDirectory(self, "Seleccionar carpeta de salida")
        if path:
            self.edit_output_dir.setText(path)

    def _current_naming_options(self) -> NamingOptions:
        return NamingOptions(
            use_proposed=self.check_use_proposed.isChecked(),
            prefix=self.edit_prefix.text().strip(),
            number_source=self.combo_number_source.currentText(),
            pad_width=self.spin_pad_width.value(),
            slug_source=self.combo_slug_source.currentText(),
            lowercase=self.check_lowercase.isChecked(),
            extension="mp3",
        )

    def _on_scan_clicked(self):
        url = self.edit_podcast_url.text().strip()
        if not url:
            self.logger.warning("Debes ingresar una URL de podcast.")
            return
        max_pages = self.spin_max_pages.value()
        pause = self.spin_pause_pages.value()
        parallel_page_fetch = self.check_parallel_scan.isChecked()
        parallel_episode_enrich = self.check_parallel_enrich.isChecked()
        auto_tune_workers = self.check_auto_workers.isChecked()
        page_workers = self.spin_page_workers.value()
        episode_workers = self.spin_episode_workers.value()
        naming_options = self._current_naming_options()

        self.busy_state_changed.emit(True)
        self.notify_signal.emit("Escaneo iniciado", "Se inició el descubrimiento de episodios del podcast.")
        self.logger.info(
            "Iniciando escaneo de podcast... "
            f"max_pages={max_pages}, parallel_pages={parallel_page_fetch}, "
            f"parallel_enrich={parallel_episode_enrich}, auto_tune={auto_tune_workers}, "
            f"page_workers_max={page_workers}, episode_workers_max={episode_workers}, "
            f"pause_pages={pause:.1f}s"
        )

        def worker():
            try:
                opts = DiscoveryOptions(
                    max_pages=max_pages,
                    pause_seconds=pause,
                    enrich_episode_pages=True,
                    parallel_page_fetch=parallel_page_fetch,
                    page_workers=page_workers,
                    parallel_episode_enrich=parallel_episode_enrich,
                    episode_workers=episode_workers,
                    auto_tune_workers=auto_tune_workers,
                    max_workers_cap=16,
                )
                eps = discover_episodes(url, options=opts, logger=self.logger)
                update_proposed_names(eps, naming_options)
                self.episodes_loaded.emit(eps)
            except Exception:
                self.logger.exception("Error durante el escaneo de podcast.")
                self.notify_signal.emit("Error en escaneo", "Ocurrió un error durante el escaneo. Revisa el log.")
                self.episodes_loaded.emit([])
            finally:
                self.busy_state_changed.emit(False)

        threading.Thread(target=worker, daemon=True).start()

    @Slot(list)
    def _on_episodes_loaded(self, eps: list):
        self.episodes = eps
        self.table.set_episodes(self.episodes)
        self.logger.info("Tabla de episodios actualizada.")

        # Al terminar el escaneo dejamos una selección válida y cargamos
        # explícitamente la primera miniatura disponible. Esto evita que el
        # panel derecho quede en "Sin selección" cuando la tabla se repuebla
        # desde un hilo y no se emite una selección nueva de forma confiable.
        if self.episodes:
            self.table.setCurrentCell(0, self.table.COL_TITLE)
            self.table.selectRow(0)
            self._load_thumbnail_for_episode(self.episodes[0], source="episodes_loaded")
        else:
            self._load_thumbnail_for_episode(None, source="episodes_loaded_empty")

        self.notify_signal.emit("Escaneo completado", f"Se cargaron {len(eps)} episodios en la tabla.")

    def _on_refresh_names(self):
        if not self.episodes:
            self.logger.warning("No hay episodios para renombrar.")
            return
        self.table.sync_from_table()
        opts = self._current_naming_options()
        update_proposed_names(self.episodes, opts)
        self.table.refresh_proposed_names()
        self.logger.info("Nombres propuestos actualizados.")

    def _on_download_clicked(self):
        if not self.episodes:
            self.logger.warning("No hay episodios cargados.")
            return
        output_dir = self.edit_output_dir.text().strip()
        if not output_dir:
            self.logger.warning("Debes seleccionar una carpeta de salida.")
            return
        out_path = Path(output_dir)
        pause = self.spin_pause_download.value()
        overwrite = self.check_overwrite.isChecked()
        embed_thumbnail = self.check_embed_thumbnail.isChecked()
        naming = self._current_naming_options()
        self.table.sync_from_table()
        selected_total = sum(1 for ep in self.episodes if ep.selected)
        if selected_total == 0:
            self.logger.warning("Debes seleccionar al menos un episodio para descargar.")
            return
        
        self.download_progress.setRange(0, selected_total)
        self.download_progress.setValue(0)
        self.download_progress.setFormat(f"0/{selected_total}")
        self.busy_state_changed.emit(True)
        self.notify_signal.emit("Descarga iniciada", f"Se descargarán {selected_total} episodios seleccionados.")
        self.logger.info("Iniciando descarga de episodios seleccionados...")
        
        def progress(idx, total, ep):
            self.logger.info(f"Progreso GUI: {idx}/{total} -> {ep.id} [{ep.download_status}]")
            self.refresh_status_signal.emit()
            self.download_progress_signal.emit(idx, total)

        episodes_snapshot = list(self.episodes)

        def worker():
            try:
                opts = DownloadOptions(
                    output_dir=out_path,
                    pause_seconds=pause,
                    overwrite=overwrite,
                    naming=naming,
                    embed_thumbnail=embed_thumbnail,
                )
                download_batch(episodes_snapshot, opts, logger=self.logger, progress_callback=progress)
                self.refresh_status_signal.emit()
                ok_count = sum(1 for ep in episodes_snapshot if getattr(ep, "selected", False) and getattr(ep, "download_status", "") == "ok")
                err_count = sum(1 for ep in episodes_snapshot if getattr(ep, "selected", False) and getattr(ep, "download_status", "") == "error")
                self.logger.info("Descarga por lote finalizada.")
                self.notify_signal.emit("Descarga finalizada", f"OK={ok_count}, errores={err_count}, total={selected_total}.")
            except Exception:
                self.logger.exception("Error durante la descarga por lote.")
                self.notify_signal.emit("Error en descarga", "Ocurrió un error durante la descarga. Revisa el log.")
            finally:
                self.busy_state_changed.emit(False)

        threading.Thread(target=worker, daemon=True).start()

    @Slot()
    def _on_refresh_status(self):
        self.table.refresh_status()
    
    @Slot(int, int)
    def _on_download_progress(self, current: int, total: int):
        if total <= 0:
            self.download_progress.setRange(0, 100)
            self.download_progress.setValue(0)
            self.download_progress.setFormat("%p%")
            return
    
        self.download_progress.setRange(0, total)
        self.download_progress.setValue(current)
        self.download_progress.setFormat(f"{current}/{total}")


    def _episode_for_row(self, row: int):
        if 0 <= row < len(self.episodes):
            return self.episodes[row]
        return None

    def _selected_episode_for_thumbnail(self):
        """Obtiene el episodio visualmente activo de forma robusta.

        Con selección extendida, selectedRows() puede devolver más de una fila
        o mantener una selección previa. Para miniaturas priorizamos currentRow(),
        que representa mejor lo que el usuario acaba de clicar.
        """
        row = self.table.currentRow()
        ep = self._episode_for_row(row)
        if ep is not None:
            return ep
        return self.table.current_episode()

    def _on_episode_cell_clicked(self, row: int, column: int):
        self._load_thumbnail_for_episode(self._episode_for_row(row), source=f"cell_clicked:{row}:{column}")

    def _on_episode_current_cell_changed(self, current_row: int, current_column: int, previous_row: int, previous_column: int):
        self._load_thumbnail_for_episode(
            self._episode_for_row(current_row),
            source=f"current_cell_changed:{previous_row}->{current_row}",
        )

    def _on_episode_selection_changed(self):
        self._load_thumbnail_for_episode(self._selected_episode_for_thumbnail(), source="selection_changed")

    def _normalize_thumbnail_url(self, url: str) -> str:
        url = (url or "").strip()
        if not url:
            return ""
        # iVoox u otros sitios pueden entregar URLs protocol-relative: //host/path.jpg
        if url.startswith("//"):
            return "https:" + url
        # También normalizamos rutas relativas defensivamente.
        return urljoin("https://www.ivoox.com/", url)

    def _load_thumbnail_for_episode(self, ep, source: str = "unknown"):
        self._thumbnail_request_id += 1
        request_id = self._thumbnail_request_id

        if ep is None:
            self.logger.debug(f"[THUMB] Sin episodio activo ({source}).")
            self.thumbnail_label.setText("Sin selección")
            self.thumbnail_label.setPixmap(QPixmap())
            return

        raw_url = getattr(ep, "thumbnail_url", None) or ""
        thumbnail_url = self._normalize_thumbnail_url(raw_url)
        if not thumbnail_url:
            self.logger.debug(f"[THUMB] Episodio sin thumbnail_url ({source}) | id={getattr(ep, 'id', '')} | {getattr(ep, 'title', '')}")
            self.thumbnail_label.setText("Sin miniatura disponible")
            self.thumbnail_label.setPixmap(QPixmap())
            return

        self.logger.debug(f"[THUMB] Cargando miniatura ({source}) | id={getattr(ep, 'id', '')} | url={thumbnail_url}")
        self.thumbnail_label.setText("Cargando miniatura...")
        self.thumbnail_label.setPixmap(QPixmap())

        def worker():
            import requests
            try:
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0 Safari/537.36"
                    ),
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    "Referer": "https://www.ivoox.com/",
                }
                resp = requests.get(thumbnail_url, timeout=15, headers=headers)
                content_type = resp.headers.get("Content-Type", "")
                if resp.ok and resp.content:
                    self.logger.debug(
                        f"[THUMB] Respuesta OK | id={getattr(ep, 'id', '')} | "
                        f"bytes={len(resp.content)} | content_type={content_type}"
                    )
                    self.thumbnail_signal.emit(request_id, resp.content)
                else:
                    self.logger.warning(
                        f"[THUMB] Respuesta no usable | id={getattr(ep, 'id', '')} | "
                        f"status={resp.status_code} | content_type={content_type}"
                    )
                    self.thumbnail_signal.emit(request_id, b"")
            except Exception as exc:
                self.logger.warning(f"[THUMB] Error descargando miniatura | id={getattr(ep, 'id', '')} | {exc}")
                self.thumbnail_signal.emit(request_id, b"")

        threading.Thread(target=worker, daemon=True).start()

    def _pixmap_from_qt_bytes(self, data: bytes):
        """Intenta decodificar la miniatura usando Qt como metodo principal.

        Retorna (pixmap, metodo). Si falla, pixmap=None.
        Se prueban varias rutas de Qt porque, en modo detached/schtasks,
        algunos plugins de imagen pueden comportarse distinto.
        """
        attempts = []
        formats = [None]
        head = data[:32]
        if head.startswith(b"\xff\xd8\xff"):
            formats.extend([b"JPG", b"JPEG"])
        elif head.startswith(b"\x89PNG\r\n\x1a\n"):
            formats.append(b"PNG")
        elif head.startswith((b"GIF87a", b"GIF89a")):
            formats.append(b"GIF")
        elif len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            formats.append(b"WEBP")

        # 1) Ruta clasica que antes funcionaba.
        for fmt in formats:
            pixmap = QPixmap()
            ok = pixmap.loadFromData(data) if fmt is None else pixmap.loadFromData(data, fmt)
            label = "auto" if fmt is None else fmt.decode("ascii", errors="ignore")
            attempts.append(f"QPixmap.loadFromData:{label}={'OK' if ok else 'FAIL'}")
            if ok and not pixmap.isNull():
                return pixmap, f"qt:QPixmap.loadFromData:{label}"

        # 2) Ruta alternativa Qt: QImage.fromData -> QPixmap.fromImage.
        for fmt in formats:
            img = QImage.fromData(data) if fmt is None else QImage.fromData(data, fmt)
            label = "auto" if fmt is None else fmt.decode("ascii", errors="ignore")
            ok = not img.isNull()
            attempts.append(f"QImage.fromData:{label}={'OK' if ok else 'FAIL'}")
            if ok:
                pixmap = QPixmap.fromImage(img)
                if not pixmap.isNull():
                    return pixmap, f"qt:QImage.fromData:{label}"

        self.logger.debug("[THUMB] Qt no pudo decodificar imagen. Intentos: " + " | ".join(attempts))
        return None, "qt:failed"

    def _pixmap_from_pillow_raw(self, data: bytes):
        """Fallback robusto: Pillow decodifica y Qt solo muestra pixeles RGBA.

        Esto evita depender de los plugins de imagen de Qt para JPEG/WEBP/etc.
        No reemplaza a Qt como primera opcion: solo se usa si Qt falla.
        """
        try:
            from PIL import Image, ImageOps
        except Exception as exc:
            self.logger.warning(f"[THUMB] Pillow no disponible para fallback: {exc}")
            return None, "pillow:not_available"

        try:
            with Image.open(BytesIO(data)) as img:
                original_mode = img.mode
                original_size = img.size
                img = ImageOps.exif_transpose(img)
                img = img.convert("RGBA")
                width, height = img.size
                raw = img.tobytes("raw", "RGBA")

            qimg = QImage(raw, width, height, width * 4, QImage.Format_RGBA8888).copy()
            if qimg.isNull():
                self.logger.warning("[THUMB] Pillow decodifico, pero QImage RGBA resulto nula.")
                return None, "pillow:rgba_qimage_null"

            pixmap = QPixmap.fromImage(qimg)
            if pixmap.isNull():
                self.logger.warning("[THUMB] Pillow decodifico, pero QPixmap.fromImage resulto nulo.")
                return None, "pillow:qpixmap_null"

            return pixmap, f"pillow:raw_rgba:{original_mode}:{original_size[0]}x{original_size[1]}"
        except Exception as exc:
            self.logger.warning(f"[THUMB] Fallback Pillow fallo al decodificar imagen: {exc}")
            return None, "pillow:decode_failed"

    def _thumbnail_decoder_policy(self, data: bytes) -> str:
        """Elige decoder segun el contexto real de ejecucion.

        - qt_first: Qt primero y Pillow fallback.
        - pillow_first: Pillow primero y Qt fallback.
        - qt_only / pillow_only: modos forzados para diagnostico.

        run_gui.py define IVOOX_THUMB_DECODER segun QImageReader.supportedImageFormats().
        En modo schtasks detached, si Qt no ve JPEG, se usa pillow_first para
        evitar entrar primero a plugins Qt rotos/lentos.
        """
        policy = (os.environ.get("IVOOX_THUMB_DECODER") or "auto").strip().lower()
        if policy in {"qt_first", "pillow_first", "qt_only", "pillow_only"}:
            return policy

        # Auto local defensivo: si es JPEG y Qt no reporta JPEG/JPG, Pillow primero.
        head = data[:16]
        is_jpeg = head.startswith(b"\xff\xd8\xff")
        if is_jpeg:
            try:
                from PySide6.QtGui import QImageReader
                fmts = {bytes(fmt).decode("ascii", errors="ignore").lower() for fmt in QImageReader.supportedImageFormats()}
                if not ({"jpg", "jpeg", "jfif"} & fmts):
                    return "pillow_first"
            except Exception:
                return "pillow_first"
        return "qt_first"

    def _decode_thumbnail_to_pixmap(self, data: bytes):
        """Decodifica miniatura con politica dinamica Qt/Pillow."""
        policy = self._thumbnail_decoder_policy(data)
        self.logger.debug(f"[THUMB] Politica de decodificacion activa: {policy}")

        if policy == "pillow_only":
            return self._pixmap_from_pillow_raw(data)
        if policy == "qt_only":
            return self._pixmap_from_qt_bytes(data)

        if policy == "pillow_first":
            pixmap, method = self._pixmap_from_pillow_raw(data)
            if pixmap is not None and not pixmap.isNull():
                return pixmap, method
            self.logger.debug(f"[THUMB] Pillow fallo primero ({method}); intentando Qt como fallback.")
            return self._pixmap_from_qt_bytes(data)

        # Default: Qt primero, Pillow fallback.
        pixmap, method = self._pixmap_from_qt_bytes(data)
        if pixmap is not None and not pixmap.isNull():
            return pixmap, method

        self.logger.debug("[THUMB] Decodificacion Qt fallo; intentando Pillow raw RGBA.")
        pixmap, method = self._pixmap_from_pillow_raw(data)
        if pixmap is not None and not pixmap.isNull():
            return pixmap, method

        return None, method

    @Slot(int, bytes)
    def _set_thumbnail_from_bytes(self, request_id: int, data: bytes):
        if request_id != self._thumbnail_request_id:
            self.logger.debug(f"[THUMB] Respuesta descartada por request_id antiguo: {request_id} != {self._thumbnail_request_id}")
            return

        if not data:
            self.thumbnail_label.setText("No se pudo cargar la miniatura")
            self.thumbnail_label.setPixmap(QPixmap())
            return

        pixmap, method = self._decode_thumbnail_to_pixmap(data)
        if pixmap is None or pixmap.isNull():
            self.logger.warning(f"[THUMB] No se pudo decodificar miniatura con Qt ni Pillow. bytes={len(data)} ultimo_metodo={method}")
            self.thumbnail_label.setText("No se pudo cargar la miniatura")
            self.thumbnail_label.setPixmap(QPixmap())
            return

        target_size = self.thumbnail_label.size()
        if target_size.width() <= 1 or target_size.height() <= 1:
            self.logger.debug(f"[THUMB] thumbnail_label sin tamaño util: {target_size.width()}x{target_size.height()}. Se usa pixmap original.")
            scaled = pixmap
        else:
            scaled = pixmap.scaled(target_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        self.thumbnail_label.setPixmap(scaled)
        self.thumbnail_label.setText("")
        self.logger.debug(
            f"[THUMB] Miniatura renderizada correctamente. metodo={method}, "
            f"original={pixmap.width()}x{pixmap.height()}, mostrada={scaled.width()}x{scaled.height()}"
        )
