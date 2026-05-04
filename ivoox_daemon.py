# -*- coding: utf-8 -*-
"""
ivoox_daemon.py
------------------------------------------------------------
Utilidades Windows para lanzar iVoox Podcast Downloader como
proceso desacoplado real de Spyder/terminal.

Version final de launcher:
- Por defecto usa schtasks como mecanismo principal de lanzamiento.
  Esto evita que la app quede atada al proceso padre de Spyder/terminal.
- Mantiene un fallback por Popen detached solo si schtasks falla.
- Usa marker CLI para detectar instancias activas.
- Escribe logs y last_ivoox_command.bat para depuracion.
- Escribe PID file en LOCALAPPDATA.
- Incluye toast Windows opcional para avisos tempranos del bootstrap.
------------------------------------------------------------
"""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Sequence

APP_MARKER_DEFAULT = "IVOOX_PODCAST_DOWNLOADER"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def is_windows() -> bool:
    return os.name == "nt"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def launcher_log_path(base_dir: Path) -> Path:
    return ensure_dir(base_dir / "logs") / "ivoox_launcher.log"


def setup_launcher_logger(base_dir: Path, level: str = "DEBUG") -> logging.Logger:
    logger = logging.getLogger("ivoox_launcher")
    logger.setLevel(getattr(logging, level.upper(), logging.DEBUG))
    logger.propagate = False

    if logger.handlers:
        return logger

    log_file = launcher_log_path(base_dir)

    class UTCFormatter(logging.Formatter):
        converter = time.gmtime

    formatter = UTCFormatter("%(asctime)sZ | %(levelname)s | %(message)s", "%Y-%m-%dT%H:%M:%S")

    fh = RotatingFileHandler(str(log_file), maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    return logger


def win_oem_encoding() -> str:
    try:
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except Exception:
        return "cp850"


def run_bytes(cmd: Sequence[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=False, timeout=timeout)


def ps_utf8(script: str) -> list[str]:
    return [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + script,
    ]


def resolve_pythonw(logger: Optional[logging.Logger] = None) -> Path:
    env = os.environ.get("IVOOX_PYTHONW_EXE", "").strip().strip('"')
    if env:
        p = Path(env)
        if p.exists():
            if logger:
                logger.debug(f"IVOOX_PYTHONW_EXE -> {p}")
            return p
        if logger:
            logger.warning(f"IVOOX_PYTHONW_EXE apunta a ruta inexistente: {p}")

    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        if logger:
            logger.debug(f"sys.executable ya es pythonw.exe -> {exe}")
        return exe

    sibling = exe.with_name("pythonw.exe")
    if sibling.exists():
        if logger:
            logger.debug(f"pythonw.exe vecino a sys.executable -> {sibling}")
        return sibling

    found = shutil.which("pythonw.exe")
    if found and Path(found).exists():
        if logger:
            logger.debug(f"PATH pythonw.exe -> {found}")
        return Path(found)

    raise FileNotFoundError(
        "No se encontro pythonw.exe. Revisa tu entorno Python/Conda o define IVOOX_PYTHONW_EXE."
    )




def infer_conda_prefix_from_pythonw(pythonw: Path) -> Optional[Path]:
    """Infiere el prefijo del entorno Conda desde .../envs/NAME/pythonw.exe.

    No activa Conda; solo identifica las carpetas necesarias para reconstruir
    PATH/QT_PLUGIN_PATH cuando el proceso nace desde schtasks.
    """
    try:
        p = Path(pythonw).resolve()
        if p.name.lower() not in {"pythonw.exe", "python.exe"}:
            return None
        prefix = p.parent
        if (prefix / "conda-meta").is_dir():
            return prefix
        return prefix
    except Exception:
        return None


def _existing_paths(paths: Sequence[Path]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        try:
            p = Path(path).resolve()
            if p.exists():
                s = str(p)
                key = s.lower()
                if key not in seen:
                    out.append(s)
                    seen.add(key)
        except Exception:
            continue
    return out


def build_conda_env_for_detached(pythonw: Path, base_env: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Reconstruye el minimo entorno Conda/Qt necesario para pythonw detached.

    Esto evita depender de que schtasks herede CONDA_PREFIX/PATH desde Spyder.
    No ejecuta `conda activate`; solo setea variables criticas y antepone
    Library/bin, Scripts y rutas de plugins Qt del entorno al PATH.
    """
    env = dict(base_env or os.environ)
    prefix = infer_conda_prefix_from_pythonw(pythonw)
    if prefix is None:
        return env

    env["CONDA_PREFIX"] = str(prefix)
    env.setdefault("CONDA_DEFAULT_ENV", prefix.name)
    env.setdefault("CONDA_DLL_SEARCH_MODIFICATION_ENABLE", "1")
    env.setdefault("IVOOX_THUMB_DECODER", "auto")

    path_dirs = _existing_paths([
        prefix,
        prefix / "Library" / "mingw-w64" / "bin",
        prefix / "Library" / "usr" / "bin",
        prefix / "Library" / "bin",
        prefix / "Scripts",
        prefix / "bin",
    ])
    old_path = env.get("PATH", "")
    env["PATH"] = os.pathsep.join(path_dirs + ([old_path] if old_path else []))

    plugin_dirs = _existing_paths([
        prefix / "Library" / "plugins",
        prefix / "Library" / "lib" / "qt6" / "plugins",
        prefix / "Lib" / "site-packages" / "PySide6" / "Qt" / "plugins",
        prefix / "Lib" / "site-packages" / "PySide6" / "Qt6" / "plugins",
    ])
    if plugin_dirs:
        old = env.get("QT_PLUGIN_PATH", "")
        env["QT_PLUGIN_PATH"] = os.pathsep.join(plugin_dirs + ([old] if old else []))

    platform_dirs = _existing_paths([Path(x) / "platforms" for x in plugin_dirs])
    if platform_dirs:
        old = env.get("QT_QPA_PLATFORM_PLUGIN_PATH", "")
        env["QT_QPA_PLATFORM_PLUGIN_PATH"] = os.pathsep.join(platform_dirs + ([old] if old else []))

    image_dirs = _existing_paths([Path(x) / "imageformats" for x in plugin_dirs])
    if image_dirs:
        env["IVOOX_QT_IMAGEFORMATS_PATHS"] = os.pathsep.join(image_dirs)

    return env


def _cmd_set_line(name: str, value: str) -> str:
    return f'set "{name}={value}"'


def _conda_cmd_env_lines(pythonw: Path) -> list[str]:
    """Genera lineas SET para el .cmd que lanzara schtasks.

    Se replica build_conda_env_for_detached sin depender de activar Conda.
    """
    env = build_conda_env_for_detached(pythonw, base_env={})
    lines: list[str] = []
    for name in [
        "IVOOX_PYTHONW_EXE",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        "CONDA_DLL_SEARCH_MODIFICATION_ENABLE",
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
        "IVOOX_QT_IMAGEFORMATS_PATHS",
        "IVOOX_THUMB_DECODER",
    ]:
        if name == "IVOOX_PYTHONW_EXE":
            lines.append(_cmd_set_line(name, str(pythonw)))
        elif env.get(name):
            lines.append(_cmd_set_line(name, env[name]))
    if env.get("PATH"):
        lines.append(_cmd_set_line("PATH", env["PATH"] + os.pathsep + "%PATH%"))
    return lines


def appdata_root() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "iVooxDownloader"
    root.mkdir(parents=True, exist_ok=True)
    return root


def pidfile_path(marker: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in marker)
    return ensure_dir(appdata_root() / "pids") / f"{safe}.pid"


def write_pidfile(marker: str, pid: Optional[int] = None) -> Path:
    pid = int(pid if pid is not None else os.getpid())
    pf = pidfile_path(marker)
    pf.write_text(str(pid), encoding="utf-8")
    return pf


def cleanup_pidfile(marker: str, pid: Optional[int] = None) -> None:
    pid = int(pid if pid is not None else os.getpid())
    pf = pidfile_path(marker)
    try:
        if pf.exists() and pf.read_text(encoding="utf-8", errors="ignore").strip() == str(pid):
            pf.unlink()
    except Exception:
        pass


def is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if not is_windows():
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False
    ps = f"if (Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}"
    try:
        r = subprocess.run(ps_utf8(ps), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
        return r.returncode == 0
    except Exception:
        return False


def _extract_pids_from_powershell_table(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.lower().startswith("processid") or set(s) <= {"-", " "}:
            continue
        parts = s.split(None, 1)
        if parts and parts[0].isdigit():
            out.append((int(parts[0]), parts[1] if len(parts) > 1 else ""))
    return out


def find_pid_by_marker(marker: str, logger: Optional[logging.Logger] = None, exclude_pid: Optional[int] = None) -> Optional[int]:
    if not marker or not is_windows():
        return None

    exclude_pid = int(exclude_pid or os.getpid())
    m = marker.replace("'", "''")
    ps_script = (
        f"$m='*{m}*'; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -like $m } | "
        "Select-Object ProcessId,CommandLine"
    )
    try:
        r = run_bytes(ps_utf8(ps_script), timeout=8)
        out = (r.stdout or b"").decode("utf-8", errors="replace")
        if logger:
            logger.debug(f"find_pid_by_marker rc={r.returncode} raw={out!r}")
        candidates = [(pid, cmd) for pid, cmd in _extract_pids_from_powershell_table(out) if pid != exclude_pid]

        for pid, cmdline in candidates:
            low = cmdline.lower()
            if "run_gui.py" in low and ("pythonw.exe" in low or "python.exe" in low):
                return pid
        for pid, cmdline in candidates:
            low = cmdline.lower()
            if "powershell" not in low and "schtasks" not in low and "cmd.exe" not in low:
                return pid
        return candidates[0][0] if candidates else None
    except Exception as exc:
        if logger:
            logger.debug(f"find_pid_by_marker fallo: {exc}")
        return None


def kill_by_pid(pid: int, logger: Optional[logging.Logger] = None) -> bool:
    if not is_windows():
        try:
            os.kill(pid, 9)
            return True
        except Exception:
            return False
    try:
        r = run_bytes(["taskkill", "/PID", str(int(pid)), "/T", "/F"], timeout=8)
        enc = win_oem_encoding()
        out = (r.stdout or b"").decode(enc, errors="replace").strip()
        err = (r.stderr or b"").decode(enc, errors="replace").strip()
        if logger:
            logger.debug(f"taskkill PID={pid} rc={r.returncode} out={out!r} err={err!r}")
        return r.returncode == 0
    except Exception as exc:
        if logger:
            logger.debug(f"kill_by_pid fallo: {exc}")
        return False


def kill_by_marker(marker: str, logger: Optional[logging.Logger] = None) -> bool:
    pid = find_pid_by_marker(marker, logger=logger)
    if not pid:
        return False
    return kill_by_pid(pid, logger=logger)


def _short_path(path_str: str) -> str:
    if not is_windows():
        return path_str
    try:
        buf = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(path_str, buf, 32768):
            return buf.value or path_str
    except Exception:
        pass
    return path_str


def _quote(x: str) -> str:
    return '"' + str(x).replace('"', '""') + '"'


@dataclass
class LaunchResult:
    ok: bool
    pid: Optional[int]
    method: str
    message: str


def write_last_command(base_dir: Path, pythonw: Path, script: Path, args: Sequence[str]) -> Path:
    logs_dir = ensure_dir(base_dir / "logs")
    bat_path = logs_dir / "last_ivoox_command.bat"
    cmd = " ".join([_quote(str(pythonw)), _quote(str(script))] + [_quote(str(a)) for a in args])
    env_lines = "\r\n".join(_conda_cmd_env_lines(pythonw))
    body = (
        "@echo off\r\n"
        f"REM {utcnow_iso()}\r\n"
        "REM Entorno reconstruido para pythonw.exe detached.\r\n"
        f"{env_lines}\r\n"
        f"cd /d {_quote(str(base_dir))}\r\n"
        f"{cmd}\r\n"
    )
    bat_path.write_text(body, encoding="utf-8")
    return bat_path


def write_scheduler_command_file(base_dir: Path, pythonw: Path, script: Path, args: Sequence[str], marker: str) -> Path:
    """Crea un .cmd corto que schtasks ejecuta.

    Ademas de fijar CWD, reconstruye variables criticas del entorno Conda/Qt.
    Esto es lo que evita que pythonw.exe nazca sin los plugins imageformats
    necesarios para JPEG/WEBP cuando se lanza desde schtasks.
    """
    logs_dir = ensure_dir(base_dir / "logs")
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in marker)
    cmd_path = logs_dir / f"run_{safe}_detached.cmd"
    command = " ".join([_quote(str(pythonw)), _quote(str(script))] + [_quote(str(a)) for a in args])
    env_lines = "\r\n".join(_conda_cmd_env_lines(pythonw))
    debug_log = logs_dir / "detached_env_debug.log"
    body = (
        "@echo off\r\n"
        f"REM {utcnow_iso()}\r\n"
        f"REM MARKER: {marker}\r\n"
        "REM Este archivo lo ejecuta schtasks. No depende de Spyder ni de Anaconda Prompt.\r\n"
        "REM Se reconstruye el entorno Conda minimo para que Qt/PySide6 cargue DLLs y plugins.\r\n"
        f"{env_lines}\r\n"
        f"echo ==== {utcnow_iso()} ==== > {_quote(str(debug_log))}\r\n"
        f"echo CONDA_PREFIX=%CONDA_PREFIX% >> {_quote(str(debug_log))}\r\n"
        f"echo QT_PLUGIN_PATH=%QT_PLUGIN_PATH% >> {_quote(str(debug_log))}\r\n"
        f"echo QT_QPA_PLATFORM_PLUGIN_PATH=%QT_QPA_PLATFORM_PLUGIN_PATH% >> {_quote(str(debug_log))}\r\n"
        f"echo IVOOX_QT_IMAGEFORMATS_PATHS=%IVOOX_QT_IMAGEFORMATS_PATHS% >> {_quote(str(debug_log))}\r\n"
        f"cd /d {_quote(str(base_dir))}\r\n"
        f"{command}\r\n"
    )
    cmd_path.write_text(body, encoding="utf-8")
    return cmd_path

def spawn_via_schtasks(
    pythonw: Path,
    script: Path,
    args: Sequence[str],
    marker: str,
    cwd: Path,
    logger: logging.Logger,
    *,
    delete_task: bool = True,
) -> Optional[int]:
    if not is_windows():
        return None

    cmd_file = write_scheduler_command_file(cwd, pythonw, script, args, marker)
    cmd_file_s = _short_path(str(cmd_file.resolve()))
    task_name = f"{marker}_RUN"

    # Usamos cmd.exe /c <cmd_file>. schtasks queda como verdadero padre,
    # no Spyder/terminal, y el .cmd deja trazabilidad reproducible.
    tr = f'cmd.exe /c {_quote(cmd_file_s)}'
    create = ["schtasks", "/Create", "/TN", task_name, "/SC", "ONCE", "/ST", "00:00", "/TR", tr, "/F"]
    run = ["schtasks", "/Run", "/TN", task_name]

    logger.info(f"[SPAWN] schtasks principal task={task_name}")
    logger.info(f"[SPAWN] comando intermedio={cmd_file}")
    logger.debug(f"[SPAWN] schtasks /TR length={len(tr)} tr={tr}")

    enc = win_oem_encoding()
    cr = run_bytes(create, timeout=12)
    logger.debug(
        "schtasks /Create rc=%s out=%r err=%r",
        cr.returncode,
        (cr.stdout or b"").decode(enc, errors="replace").strip(),
        (cr.stderr or b"").decode(enc, errors="replace").strip(),
    )
    if cr.returncode != 0:
        return None

    rr = run_bytes(run, timeout=12)
    logger.debug(
        "schtasks /Run rc=%s out=%r err=%r",
        rr.returncode,
        (rr.stdout or b"").decode(enc, errors="replace").strip(),
        (rr.stderr or b"").decode(enc, errors="replace").strip(),
    )
    if rr.returncode != 0:
        return None

    deadline = time.time() + 25.0
    pid_found = None
    while time.time() < deadline:
        pid = find_pid_by_marker(marker, logger=logger)
        if pid and is_running(pid):
            pid_found = pid
            break
        time.sleep(0.7)

    if delete_task:
        try:
            dr = run_bytes(["schtasks", "/Delete", "/TN", task_name, "/F"], timeout=8)
            logger.debug("schtasks /Delete rc=%s", dr.returncode)
        except Exception as exc:
            logger.debug(f"No se pudo borrar task temporal {task_name}: {exc}")

    return pid_found


def spawn_detached_process(
    pythonw: Path,
    script: Path,
    args: Sequence[str],
    cwd: Path,
    logger: logging.Logger,
) -> Optional[int]:
    cmd = [str(pythonw), str(script), *map(str, args)]
    logger.warning("[SPAWN] Fallback direct process. Puede quedar atado al padre si Spyder/terminal usa Job Objects.")
    logger.info(f"[SPAWN] Direct process: {' '.join(_quote(c) if ' ' in c else c for c in cmd)}")

    env = build_conda_env_for_detached(pythonw, os.environ.copy())
    env["IVOOX_DETACHED_PARENT_PID"] = str(os.getpid())

    if is_windows():
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            close_fds=True,
            startupinfo=startupinfo,
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    else:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return int(proc.pid)


def launch_detached_app(
    script: Path,
    *,
    marker: str = APP_MARKER_DEFAULT,
    start_hidden: bool = False,
    extra_args: Optional[Sequence[str]] = None,
    logger: Optional[logging.Logger] = None,
    prefer_schtasks: bool = True,
) -> LaunchResult:
    script = Path(script).resolve()
    base_dir = script.parent
    logger = logger or setup_launcher_logger(base_dir)

    logger.info("===== IVOOX LAUNCH REQUEST =====")
    logger.info(f"script={script}")
    logger.info(f"marker={marker}")
    logger.info(f"sys.executable={sys.executable}")
    logger.info(f"cwd={base_dir}")
    logger.info(f"prefer_schtasks={prefer_schtasks}, start_hidden={start_hidden}")

    if not script.exists():
        msg = f"run_gui.py no existe: {script}"
        logger.error(msg)
        return LaunchResult(False, None, "none", msg)

    existing = find_pid_by_marker(marker, logger=logger)
    if existing and is_running(existing):
        msg = f"Ya existe una instancia activa con marker={marker}, PID={existing}."
        logger.warning(msg)
        return LaunchResult(True, existing, "existing", msg)

    try:
        pythonw = resolve_pythonw(logger)
    except Exception as exc:
        msg = f"No se pudo resolver pythonw.exe: {exc}"
        logger.exception(msg)
        return LaunchResult(False, None, "none", msg)

    args = ["--ivoox-detached-child", "--ivoox-marker", marker]
    if start_hidden:
        args.append("--start-hidden")
    if extra_args:
        args.extend(str(a) for a in extra_args)

    prefix = infer_conda_prefix_from_pythonw(pythonw)
    logger.info(f"pythonw resuelto: {pythonw}")
    logger.info(f"conda_prefix_inferido_desde_pythonw: {prefix or '(no detectado)'}")

    last_cmd = write_last_command(base_dir, pythonw, script, args)
    logger.debug(f"last command escrito en {last_cmd}")

    methods: list[str] = ["schtasks", "process"] if prefer_schtasks and is_windows() else ["process", "schtasks"]

    for method in methods:
        if method == "schtasks":
            try:
                pid = spawn_via_schtasks(pythonw, script, args, marker=marker, cwd=base_dir, logger=logger)
                if pid and is_running(pid):
                    write_pidfile(marker, pid)
                    msg = f"Instancia detached iniciada correctamente. PID={pid} via schtasks."
                    logger.info(msg)
                    return LaunchResult(True, pid, "schtasks", msg)
                logger.warning("[SPAWN] schtasks no confirmo PID vivo.")
            except Exception:
                logger.exception("[SPAWN] Fallo spawn_via_schtasks.")

        elif method == "process":
            try:
                pid0 = spawn_detached_process(pythonw, script, args, cwd=base_dir, logger=logger)
                logger.info(f"[SPAWN] PID directo={pid0}; verificando vida del proceso...")
                time.sleep(1.5)
                pid_by_marker = find_pid_by_marker(marker, logger=logger)
                pid_eff = pid_by_marker or pid0
                if pid_eff and is_running(pid_eff):
                    write_pidfile(marker, pid_eff)
                    msg = f"Instancia detached iniciada correctamente. PID={pid_eff} via process."
                    logger.warning(msg + " Este metodo puede no sobrevivir cierre de Spyder en algunos entornos.")
                    return LaunchResult(True, pid_eff, "process", msg)
                logger.warning("[SPAWN] El proceso directo no quedo vivo.")
            except Exception:
                logger.exception("[SPAWN] Fallo spawn_detached_process.")

    msg = "No se pudo iniciar instancia detached por schtasks ni process. Revisa logs/ivoox_launcher.log."
    logger.error(msg)
    return LaunchResult(False, None, "failed", msg)


def show_windows_toast(
    title: str,
    message: str,
    *,
    duration: int = 5,
    on_click_open: Optional[str | Path] = None,
    logger: Optional[logging.Logger] = None,
) -> bool:
    if not is_windows():
        return False
    try:
        import textwrap

        def esc(s: str) -> str:
            return str(s).replace("`", "``").replace('"', '`"')

        title_ps = esc(title)
        message_ps = esc(message)
        path_ps = esc(str(on_click_open)) if on_click_open else ""
        ms = int(max(1, duration) * 1000)

        ps = textwrap.dedent(
            f'''
            Add-Type -AssemblyName System.Windows.Forms
            Add-Type -AssemblyName System.Drawing
            $n = New-Object System.Windows.Forms.NotifyIcon
            $n.Icon = [System.Drawing.SystemIcons]::Information
            $n.BalloonTipTitle = "{title_ps}"
            $n.BalloonTipText  = "{message_ps}"
            $n.Visible = $true
            $p = "{path_ps}"
            if ($p -ne "") {{
                $n.add_BalloonTipClicked({{
                    try {{ Start-Process -FilePath explorer.exe -ArgumentList @($p) }} catch {{ }}
                }})
            }}
            [System.Windows.Forms.Application]::DoEvents()
            $n.ShowBalloonTip({ms})
            for ($t = 0; $t -lt ({ms} + 800); $t += 120) {{
                Start-Sleep -Milliseconds 120
                [System.Windows.Forms.Application]::DoEvents()
            }}
            $n.Dispose()
            '''
        )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            startupinfo=startupinfo,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return True
    except Exception as exc:
        if logger:
            logger.debug(f"show_windows_toast fallo: {exc}")
        return False
