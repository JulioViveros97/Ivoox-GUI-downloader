# -*- coding: utf-8 -*-
"""
AUX_env_installer_ivoox_tk.py
------------------------------------------------------------
Instalador / diagnostico de entorno para iVoox Podcast Downloader.

Version GUI liviana basada en Tkinter, sin PySide6.

Objetivos:
A) Diagnosticar Python, pip, conda y entorno activo.
B) Verificar dependencias y versiones minimas.
C) Proponer instalaciones sin ejecutarlas automaticamente.
D) Instalar PySide6 mediante conda, si hay conda disponible.
E) Instalar dependencias simples mediante pip.
F) Validar estructura minima del proyecto.
G) Ejecutar smoke tests de imports del proyecto.
H) Permitir abrir run_gui.py desde la propia interfaz.

Politica de seguridad:
- No instala nada al abrir.
- No reinstala dependencias ya disponibles y con version aceptable.
- No usa pip --upgrade por defecto.
- PySide6 se maneja preferentemente con conda, no con pip.
- Las acciones de instalacion requieren confirmacion explicita del usuario.

Uso:
    python AUX_env_installer_ivoox_tk.py

Recomendado desde Anaconda Prompt:
    conda activate GEOF
    python AUX_env_installer_ivoox_tk.py
------------------------------------------------------------
"""

from __future__ import annotations

import importlib
import importlib.metadata as md
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# ---------------------------------------------------------------------
# 1) Configuracion
# ---------------------------------------------------------------------

APP_NAME = "iVoox Podcast Downloader - Instalador de entorno"

# package_name: nombre para instalar; import_name: nombre para importar;
# min_version: version minima sugerida; installer: conda o pip.
@dataclass(frozen=True)
class Requirement:
    package_name: str
    import_name: str
    min_version: str
    installer: str  # "conda" o "pip"
    conda_channel: Optional[str] = None
    note: str = ""


REQUIREMENTS: List[Requirement] = [
    Requirement(
        package_name="pyside6",
        import_name="PySide6",
        min_version="6.6.0",
        installer="conda",
        conda_channel="conda-forge",
        note="GUI principal. Se recomienda instalar con conda para evitar conflictos Qt/DLL.",
    ),
    Requirement(
        package_name="requests",
        import_name="requests",
        min_version="2.31.0",
        installer="pip",
        note="Descargas HTTP.",
    ),
    Requirement(
        package_name="beautifulsoup4",
        import_name="bs4",
        min_version="4.12.0",
        installer="pip",
        note="Parseo HTML. El import real es bs4.",
    ),
    Requirement(
        package_name="mutagen",
        import_name="mutagen",
        min_version="1.47.0",
        installer="pip",
        note="Metadatos de audio.",
    ),
    Requirement(
        package_name="Pillow",
        import_name="PIL",
        min_version="10.0.0",
        installer="pip",
        note="Fallback robusto para decodificar miniaturas JPEG/WEBP cuando Qt no puede hacerlo.",
    ),
]

REQUIRED_PATHS: Dict[str, str] = {
    "kernel": "dir",
    "gui": "dir",
    "run_gui.py": "file",
    "kernel/episode_model.py": "file",
    "kernel/naming_schemes.py": "file",
    "kernel/logging_utils.py": "file",
    "kernel/ivox_discovery.py": "file",
    "kernel/ivox_download.py": "file",
    "gui/collapsible_box.py": "file",
    "gui/episodes_table.py": "file",
    "gui/thumbnail_loader.py": "file",
    "gui/workers.py": "file",
    "gui/main_window.py": "file",
}

SMOKE_IMPORTS: List[str] = [
    "kernel.episode_model",
    "kernel.naming_schemes",
    "kernel.logging_utils",
    "kernel.ivox_discovery",
    "kernel.ivox_download",
    "gui.collapsible_box",
    "gui.episodes_table",
    "gui.thumbnail_loader",
    "gui.workers",
    "gui.main_window",
]

# Modulos retirados de la libreria estandar en Python moderno que conviene
# reportar como incompatibilidad de codigo, no como simple dependencia faltante.
REMOVED_STDLIB_MODULES: Dict[str, str] = {
    "imghdr": "Removido en Python 3.13. Reemplazar por deteccion de magic bytes, filetype, puremagic o python-magic.",
}

PROJECT_SOURCE_GLOBS: Tuple[str, ...] = (
    "kernel/*.py",
    "gui/*.py",
    "*.py",
)


# ---------------------------------------------------------------------
# 2) Utilidades generales
# ---------------------------------------------------------------------

def now() -> str:
    return time.strftime("%H:%M:%S")


def is_conda_env() -> bool:
    return bool(os.environ.get("CONDA_PREFIX"))


def conda_env_name() -> Optional[str]:
    return os.environ.get("CONDA_DEFAULT_ENV")


def conda_prefix() -> Optional[str]:
    return os.environ.get("CONDA_PREFIX")


def find_conda_executable() -> Optional[str]:
    # En Anaconda Prompt normalmente existe CONDA_EXE.
    conda_exe = os.environ.get("CONDA_EXE")
    if conda_exe and Path(conda_exe).exists():
        return conda_exe

    # Fallback: buscar en PATH.
    found = shutil.which("conda")
    if found:
        return found
    return None


def parse_version_safe(version: str) -> Tuple[int, ...]:
    """Parseo simple y conservador para comparar versiones tipo 6.6.0.

    No pretende reemplazar packaging.version, para no agregar dependencia extra.
    Ignora sufijos no numericos.
    """
    parts: List[int] = []
    for chunk in str(version).replace("-", ".").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            parts.append(int(digits))
        else:
            break
    return tuple(parts) if parts else (0,)


def version_ge(installed: str, minimum: str) -> bool:
    a = parse_version_safe(installed)
    b = parse_version_safe(minimum)
    max_len = max(len(a), len(b))
    a = a + (0,) * (max_len - len(a))
    b = b + (0,) * (max_len - len(b))
    return a >= b


def get_distribution_version(req: Requirement) -> Optional[str]:
    candidates = [req.package_name, req.import_name]

    # Casos conocidos donde nombre de distribucion != import.
    if req.import_name == "bs4":
        candidates.insert(0, "beautifulsoup4")
    if req.import_name == "PySide6":
        candidates.insert(0, "PySide6")
    if req.import_name == "PIL":
        candidates.insert(0, "Pillow")

    for name in candidates:
        try:
            return md.version(name)
        except md.PackageNotFoundError:
            continue
        except Exception:
            continue
    return None


def try_import_module(import_name: str) -> Tuple[bool, Optional[str]]:
    try:
        importlib.import_module(import_name)
        return True, None
    except ModuleNotFoundError as exc:
        return False, f"ModuleNotFoundError: {exc}"
    except ImportError as exc:
        return False, f"ImportError: {exc}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def run_subprocess_stream(
    cmd: List[str],
    cwd: Optional[Path],
    log: Callable[[str, str], None],
) -> int:
    log("CMD", " ".join(f'"{x}"' if " " in x else x for x in cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            universal_newlines=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.strip():
                log("OUT", line)
        return int(proc.wait())
    except Exception as exc:
        log("ERROR", f"No se pudo ejecutar comando: {exc}")
        return 1


# ---------------------------------------------------------------------
# 3) Diagnostico y plan de acciones
# ---------------------------------------------------------------------

@dataclass
class RequirementStatus:
    req: Requirement
    import_ok: bool
    import_error: Optional[str]
    installed_version: Optional[str]
    version_ok: Optional[bool]
    action: str  # ok, install, upgrade, manual_review
    reason: str


@dataclass
class DiagnosisResult:
    requirement_statuses: List[RequirementStatus]
    structure_ok: bool
    missing_paths: List[str]
    smoke_ok: Optional[bool]
    smoke_failures: List[Tuple[str, str]]
    python_ok: bool
    conda_available: bool
    conda_env_active: bool
    compatibility_issues: List[str]
    warnings: List[str]


def diagnose_requirements(strict_versions: bool) -> List[RequirementStatus]:
    statuses: List[RequirementStatus] = []
    for req in REQUIREMENTS:
        import_ok, import_error = try_import_module(req.import_name)
        version = get_distribution_version(req)
        v_ok: Optional[bool] = None
        if version is not None:
            v_ok = version_ge(version, req.min_version)

        if import_ok and (v_ok is True or not strict_versions or version is None):
            action = "ok"
            reason = "Import OK"
            if version:
                reason += f"; version detectada {version}"
            if version and v_ok is False and not strict_versions:
                reason += f"; bajo minimo sugerido {req.min_version}, pero strict_versions=False"
        elif import_ok and v_ok is False and strict_versions:
            action = "upgrade"
            reason = f"Import OK, pero version {version} < minimo {req.min_version}"
        elif not import_ok and "ModuleNotFoundError" in str(import_error):
            action = "install"
            reason = f"No instalado o no visible para este Python: {import_error}"
        else:
            action = "manual_review"
            reason = f"El paquete existe o intenta cargar, pero falla el import: {import_error}"

        statuses.append(
            RequirementStatus(
                req=req,
                import_ok=import_ok,
                import_error=import_error,
                installed_version=version,
                version_ok=v_ok,
                action=action,
                reason=reason,
            )
        )
    return statuses


def diagnose_structure(project_root: Path) -> Tuple[bool, List[str]]:
    missing: List[str] = []
    for rel, kind in REQUIRED_PATHS.items():
        p = project_root / rel
        if kind == "dir" and not p.is_dir():
            missing.append(rel)
        elif kind == "file" and not p.is_file():
            missing.append(rel)
    return (len(missing) == 0), missing


def run_smoke_tests(project_root: Path) -> Tuple[bool, List[Tuple[str, str]]]:
    failures: List[Tuple[str, str]] = []
    root_str = str(project_root.resolve())
    inserted = False
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
        inserted = True

    try:
        for mod in SMOKE_IMPORTS:
            ok, err = try_import_module(mod)
            if not ok:
                failures.append((mod, err or "Error desconocido"))
    finally:
        # No removemos necesariamente el path para evitar efectos raros si se lanzan pruebas posteriores.
        # Pero se deja anotado por claridad.
        _ = inserted

    return (len(failures) == 0), failures


def scan_removed_stdlib_usage(project_root: Path) -> List[str]:
    """Busca imports de modulos retirados de la stdlib en archivos del proyecto."""
    issues: List[str] = []
    seen: set[Path] = set()

    for pattern in PROJECT_SOURCE_GLOBS:
        for file_path in project_root.glob(pattern):
            if not file_path.is_file() or file_path in seen:
                continue
            seen.add(file_path)
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception as exc:
                issues.append(f"No se pudo leer {file_path.relative_to(project_root)}: {exc}")
                continue

            for module_name, note in REMOVED_STDLIB_MODULES.items():
                pat = re.compile(rf"^\s*(?:import\s+{re.escape(module_name)}\b|from\s+{re.escape(module_name)}\s+import\s+)", re.MULTILINE)
                if pat.search(text):
                    rel = file_path.relative_to(project_root)
                    issues.append(f"{rel}: usa '{module_name}'. {note}")

    return issues


def build_diagnosis(project_root: Path, strict_versions: bool, smoke: bool) -> DiagnosisResult:
    warnings: List[str] = []
    conda_exe = find_conda_executable()
    conda_avail = conda_exe is not None
    conda_active = is_conda_env()

    if not conda_active:
        warnings.append(
            "No se detecta CONDA_PREFIX. Probablemente no estas dentro de un entorno conda activo. "
            "Para PySide6 se recomienda ejecutar desde Anaconda Prompt con 'conda activate <entorno>'."
        )
    if not conda_avail:
        warnings.append(
            "No se encontro el ejecutable conda. PySide6 no podra instalarse automaticamente con conda desde esta GUI."
        )

    py_ok = sys.version_info >= (3, 9)
    if not py_ok:
        warnings.append("Python menor a 3.9. El proyecto podria requerir una version mas reciente.")

    reqs = diagnose_requirements(strict_versions=strict_versions)
    struct_ok, missing = diagnose_structure(project_root)
    compatibility_issues = scan_removed_stdlib_usage(project_root)

    smoke_ok: Optional[bool] = None
    smoke_failures: List[Tuple[str, str]] = []
    if smoke:
        smoke_ok, smoke_failures = run_smoke_tests(project_root)

    return DiagnosisResult(
        requirement_statuses=reqs,
        structure_ok=struct_ok,
        missing_paths=missing,
        smoke_ok=smoke_ok,
        smoke_failures=smoke_failures,
        python_ok=py_ok,
        conda_available=conda_avail,
        conda_env_active=conda_active,
        compatibility_issues=compatibility_issues,
        warnings=warnings,
    )


# ---------------------------------------------------------------------
# 4) GUI Tkinter
# ---------------------------------------------------------------------

class InstallerTkApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1120x780")
        self.minsize(980, 650)

        self.msg_queue: queue.Queue[Tuple[str, str]] = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.last_diagnosis: Optional[DiagnosisResult] = None

        self.project_root_var = tk.StringVar(value=str(Path(__file__).resolve().parent))
        self.strict_versions_var = tk.BooleanVar(value=False)
        self.smoke_var = tk.BooleanVar(value=True)
        self.install_confirmed_var = tk.BooleanVar(value=False)

        self._build_ui()
        self.after(100, self._poll_queue)
        self.log("INFO", "Instalador iniciado. Primero ejecuta 'Diagnosticar'. No se instalara nada automaticamente.")

    # -------------------------- UI --------------------------
    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        top = ttk.Frame(self, padding=10)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Raiz del proyecto:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.project_root_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(top, text="Examinar...", command=self.browse_root).grid(row=0, column=2, sticky="e")

        opts = ttk.Frame(self, padding=(10, 0, 10, 8))
        opts.grid(row=1, column=0, sticky="ew")

        ttk.Checkbutton(
            opts,
            text="Verificar versiones minimas estrictamente",
            variable=self.strict_versions_var,
        ).grid(row=0, column=0, sticky="w", padx=(0, 20))

        ttk.Checkbutton(
            opts,
            text="Ejecutar smoke tests del proyecto",
            variable=self.smoke_var,
        ).grid(row=0, column=1, sticky="w", padx=(0, 20))

        ttk.Checkbutton(
            opts,
            text="Confirmo que deseo permitir instalaciones al presionar Instalar acciones propuestas",
            variable=self.install_confirmed_var,
        ).grid(row=0, column=2, sticky="w")

        main = ttk.PanedWindow(self, orient=tk.VERTICAL)
        main.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 10))

        upper = ttk.PanedWindow(main, orient=tk.HORIZONTAL)
        main.add(upper, weight=3)

        left = ttk.Frame(upper)
        right = ttk.Frame(upper)
        upper.add(left, weight=2)
        upper.add(right, weight=3)

        # Tabla de dependencias
        ttk.Label(left, text="Dependencias").pack(anchor="w")
        cols = ("package", "import", "installer", "version", "status", "action")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", height=10)
        headings = {
            "package": "Paquete",
            "import": "Import",
            "installer": "Instalador",
            "version": "Version",
            "status": "Estado",
            "action": "Accion",
        }
        widths = {
            "package": 130,
            "import": 110,
            "installer": 90,
            "version": 90,
            "status": 170,
            "action": 100,
        }
        for c in cols:
            self.tree.heading(c, text=headings[c])
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.pack(fill="both", expand=True, pady=(4, 8))

        # Estado de entorno
        ttk.Label(right, text="Resumen de entorno y validaciones").pack(anchor="w")
        self.summary = tk.Text(right, height=12, wrap="word")
        self.summary.pack(fill="both", expand=True, pady=(4, 8))
        self.summary.configure(state="disabled")

        # Log
        log_frame = ttk.Frame(main)
        main.add(log_frame, weight=2)
        ttk.Label(log_frame, text="Log").pack(anchor="w")
        self.log_text = tk.Text(log_frame, height=14, wrap="none")
        self.log_text.pack(side="left", fill="both", expand=True)
        yscroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        yscroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=yscroll.set, state="disabled")

        # Botones
        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.grid(row=3, column=0, sticky="ew")

        self.btn_diagnose = ttk.Button(buttons, text="Diagnosticar", command=self.start_diagnosis)
        self.btn_diagnose.pack(side="left", padx=(0, 8))

        self.btn_install = ttk.Button(buttons, text="Instalar acciones propuestas", command=self.start_install_actions)
        self.btn_install.pack(side="left", padx=(0, 8))

        self.btn_launch = ttk.Button(buttons, text="Abrir iVoox Downloader", command=self.launch_app)
        self.btn_launch.pack(side="left", padx=(0, 8))

        self.btn_copy = ttk.Button(buttons, text="Copiar log", command=self.copy_log)
        self.btn_copy.pack(side="left", padx=(0, 8))

        self.btn_clear = ttk.Button(buttons, text="Limpiar log", command=self.clear_log)
        self.btn_clear.pack(side="left", padx=(0, 8))

        self.progress = ttk.Progressbar(buttons, mode="indeterminate")
        self.progress.pack(side="right", fill="x", expand=True, padx=(8, 0))

    # -------------------------- logging/thread --------------------------
    def log(self, level: str, message: str) -> None:
        self.msg_queue.put((level, message))

    def _poll_queue(self) -> None:
        try:
            while True:
                level, message = self.msg_queue.get_nowait()
                self._append_log(level, message)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _append_log(self, level: str, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{now()}] [{level}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for btn in [self.btn_diagnose, self.btn_install, self.btn_launch, self.btn_copy, self.btn_clear]:
            btn.configure(state=state)
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    def run_in_worker(self, target: Callable[[], None]) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Proceso en curso", "Ya hay una tarea ejecutandose.")
            return
        self.set_busy(True)

        def wrapped() -> None:
            try:
                target()
            except Exception:
                self.log("ERROR", "Excepcion no controlada:")
                for line in traceback.format_exc().splitlines():
                    self.log("ERROR", line)
            finally:
                self.after(0, lambda: self.set_busy(False))

        self.worker_thread = threading.Thread(target=wrapped, daemon=True)
        self.worker_thread.start()

    # -------------------------- acciones UI --------------------------
    def browse_root(self) -> None:
        selected = filedialog.askdirectory(title="Seleccionar raiz del proyecto")
        if selected:
            self.project_root_var.set(selected)

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def copy_log(self) -> None:
        text = self.log_text.get("1.0", "end").strip()
        self.clipboard_clear()
        self.clipboard_append(text)
        messagebox.showinfo("Log copiado", "El log fue copiado al portapapeles.")

    def start_diagnosis(self) -> None:
        self.run_in_worker(self._diagnose_worker)

    def _diagnose_worker(self) -> None:
        root = Path(self.project_root_var.get().strip()).resolve()
        self.log("INFO", "=== Diagnostico de entorno ===")
        self.log("INFO", f"sys.executable: {sys.executable}")
        self.log("INFO", f"Python: {platform.python_version()} ({platform.python_implementation()})")
        self.log("INFO", f"Platform: {platform.platform()}")
        self.log("INFO", f"CONDA_DEFAULT_ENV: {conda_env_name() or '(no detectado)'}")
        self.log("INFO", f"CONDA_PREFIX: {conda_prefix() or '(no detectado)'}")
        self.log("INFO", f"conda executable: {find_conda_executable() or '(no encontrado)'}")
        self.log("INFO", f"Project root: {root}")

        result = build_diagnosis(
            project_root=root,
            strict_versions=self.strict_versions_var.get(),
            smoke=self.smoke_var.get(),
        )
        self.last_diagnosis = result
        self.after(0, lambda: self.render_diagnosis(result))
        self.log("INFO", "Diagnostico completado. No se instalo ni modifico nada.")

    def render_diagnosis(self, result: DiagnosisResult) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

        for st in result.requirement_statuses:
            if st.action == "ok":
                status = "OK"
            elif st.action == "install":
                status = "Faltante"
            elif st.action == "upgrade":
                status = "Version baja"
            else:
                status = "Revisar manual"

            self.tree.insert(
                "",
                "end",
                values=(
                    st.req.package_name,
                    st.req.import_name,
                    st.req.installer,
                    st.installed_version or "(no detectada)",
                    status,
                    st.action,
                ),
            )

        lines: List[str] = []
        lines.append("ENTORNO")
        lines.append(f"- Python: {platform.python_version()} ({'OK' if result.python_ok else 'REVISAR'})")
        lines.append(f"- Ejecutable: {sys.executable}")
        lines.append(f"- Conda disponible: {'SI' if result.conda_available else 'NO'}")
        lines.append(f"- Entorno conda activo: {'SI' if result.conda_env_active else 'NO'}")
        lines.append(f"- Nombre entorno: {conda_env_name() or '(no detectado)'}")
        lines.append("")

        lines.append("ESTRUCTURA")
        lines.append(f"- Estado: {'OK' if result.structure_ok else 'INCOMPLETA'}")
        if result.missing_paths:
            lines.append("- Faltantes:")
            for rel in result.missing_paths:
                lines.append(f"  * {rel}")
        lines.append("")

        lines.append("COMPATIBILIDAD PYTHON")
        if result.compatibility_issues:
            lines.append("- Revisar:")
            for issue in result.compatibility_issues:
                lines.append(f"  * {issue}")
        else:
            lines.append("- OK: no se detectaron imports conocidos de stdlib removida.")
        lines.append("")

        lines.append("SMOKE TESTS")
        if result.smoke_ok is None:
            lines.append("- No ejecutados")
        elif result.smoke_ok:
            lines.append("- OK")
        else:
            lines.append("- Fallas:")
            for mod, err in result.smoke_failures:
                lines.append(f"  * {mod}: {err}")
        lines.append("")

        proposed = [st for st in result.requirement_statuses if st.action in {"install", "upgrade"}]
        manual = [st for st in result.requirement_statuses if st.action == "manual_review"]

        lines.append("ACCIONES PROPUESTAS")
        if not proposed and not manual:
            lines.append("- No hay acciones de instalacion propuestas.")
        for st in proposed:
            lines.append(f"- {st.action.upper()}: {st.req.package_name} via {st.req.installer}. Motivo: {st.reason}")
        for st in manual:
            lines.append(f"- REVISION MANUAL: {st.req.package_name}. Motivo: {st.reason}")
        lines.append("")

        if result.warnings:
            lines.append("ADVERTENCIAS")
            for w in result.warnings:
                lines.append(f"- {w}")

        self.summary.configure(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("end", "\n".join(lines))
        self.summary.configure(state="disabled")

    def start_install_actions(self) -> None:
        if not self.install_confirmed_var.get():
            messagebox.showwarning(
                "Confirmacion requerida",
                "Marca la casilla de confirmacion antes de instalar. Esto evita modificaciones accidentales del entorno.",
            )
            return

        if self.last_diagnosis is None:
            messagebox.showwarning("Diagnostico requerido", "Primero ejecuta Diagnosticar.")
            return

        actions = [
            st for st in self.last_diagnosis.requirement_statuses
            if st.action in {"install", "upgrade"}
        ]
        if not actions:
            messagebox.showinfo("Sin acciones", "No hay dependencias faltantes o desactualizadas que instalar.")
            return

        detail = "\n".join(f"- {st.req.package_name} via {st.req.installer} ({st.action})" for st in actions)
        ok = messagebox.askyesno(
            "Confirmar instalacion",
            "Se ejecutaran las siguientes acciones en el entorno Python actual:\n\n"
            f"{detail}\n\n"
            f"Python activo:\n{sys.executable}\n\n"
            "Quieres continuar?",
        )
        if not ok:
            return

        self.run_in_worker(lambda: self._install_worker(actions))

    def _install_worker(self, actions: List[RequirementStatus]) -> None:
        root = Path(self.project_root_var.get().strip()).resolve()
        self.log("INFO", "=== Instalacion de acciones propuestas ===")
        self.log("INFO", f"Python activo: {sys.executable}")
        self.log("INFO", f"Entorno conda: {conda_env_name() or '(no detectado)'}")

        for st in actions:
            req = st.req
            self.log("INFO", f"Procesando {req.package_name} ({st.action})")

            # Revalidacion justo antes de instalar para evitar tocar si ya quedo OK.
            import_ok, import_err = try_import_module(req.import_name)
            version = get_distribution_version(req)
            if import_ok and version and version_ge(version, req.min_version):
                self.log("INFO", f"Saltado: {req.import_name} ya importa y version {version} >= {req.min_version}")
                continue
            if import_ok and st.action == "install":
                self.log("INFO", f"Saltado: {req.import_name} ya importa. No se reinstala.")
                continue

            if req.installer == "conda":
                rc = self._install_with_conda(req)
            elif req.installer == "pip":
                rc = self._install_with_pip(req, upgrade=(st.action == "upgrade"))
            else:
                self.log("ERROR", f"Instalador desconocido para {req.package_name}: {req.installer}")
                rc = 1

            if rc != 0:
                self.log("ERROR", f"Fallo instalacion de {req.package_name} con codigo {rc}")
            else:
                self.log("INFO", f"Instalacion finalizada para {req.package_name}")

        self.log("INFO", "Re-ejecutando diagnostico despues de instalar...")
        result = build_diagnosis(
            project_root=root,
            strict_versions=self.strict_versions_var.get(),
            smoke=self.smoke_var.get(),
        )
        self.last_diagnosis = result
        self.after(0, lambda: self.render_diagnosis(result))
        self.log("INFO", "Proceso de instalacion terminado.")

    def _install_with_conda(self, req: Requirement) -> int:
        conda_exe = find_conda_executable()
        env_name = conda_env_name()

        if not conda_exe:
            self.log("ERROR", "No se encontro conda. No se instalara PySide6 con pip automaticamente.")
            self.log("INFO", "Solucion sugerida: abrir Anaconda Prompt, activar entorno y ejecutar: conda install -c conda-forge pyside6")
            return 1

        if not env_name:
            self.log("ERROR", "No se detecta CONDA_DEFAULT_ENV. Activa el entorno correcto antes de instalar con conda.")
            self.log("INFO", "Ejemplo: conda activate GEOF")
            return 1

        cmd = [conda_exe, "install", "-n", env_name]
        if req.conda_channel:
            cmd.extend(["-c", req.conda_channel])
        cmd.extend([req.package_name, "-y"])
        return run_subprocess_stream(cmd, cwd=None, log=self.log)

    def _install_with_pip(self, req: Requirement, upgrade: bool = False) -> int:
        cmd = [sys.executable, "-m", "pip", "install"]
        if upgrade:
            cmd.append("--upgrade")
        cmd.append(req.package_name)
        return run_subprocess_stream(cmd, cwd=None, log=self.log)

    def launch_app(self) -> None:
        root = Path(self.project_root_var.get().strip()).resolve()
        run_gui = root / "run_gui.py"
        if not run_gui.is_file():
            messagebox.showwarning("No encontrado", f"No existe:\n{run_gui}")
            return

        try:
            subprocess.Popen([sys.executable, str(run_gui)], cwd=str(root), close_fds=True)
            messagebox.showinfo("Aplicacion lanzada", "Se intento abrir iVoox Podcast Downloader.")
        except Exception as exc:
            messagebox.showerror("Error", f"No se pudo abrir la aplicacion:\n{exc}")


# ---------------------------------------------------------------------
# 5) Main
# ---------------------------------------------------------------------

def main() -> int:
    app = InstallerTkApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
