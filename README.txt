README - iVoox Podcast Downloader
=================================

Descripcion breve
-----------------
iVoox Podcast Downloader es una aplicacion de escritorio en Python orientada a descubrir, ordenar, previsualizar y descargar episodios de podcasts desde iVoox.

SE BUSCA COLABORACIÓN: El codigo ya es funcional, pero no sé como transformarlo a un .exe mas sencillo de compartir.


El modulo permite:

- Escanear paginas de episodios de un podcast de iVoox.
- Descubrir episodios en orden cronologico.
- Enriquecer cada episodio con titulo, fecha, duracion y miniatura.
- Ajustar dinamicamente workers de paginas y de enriquecimiento segun equipo/latencia.
- Previsualizar miniaturas en la GUI.
- Proponer nombres de archivo ordenados y editables.
- Descargar episodios seleccionados.
- Embeber portada/thumbnail en los metadatos ID3 del MP3.
- Ejecutarse como aplicacion desacoplada de Spyder/terminal mediante run_gui.py.
- Mantenerse activa en segundo plano como tray app en Windows.

La aplicacion fue pensada inicialmente para uso en entorno Anaconda/Conda durante desarrollo, pero su arquitectura apunta a una futura version empaquetada como .exe.


Requisitos principales
----------------------

Dependencias Python esperadas:

- PySide6
- requests
- beautifulsoup4 / bs4
- mutagen
- Pillow

En desarrollo se recomienda usar un entorno Conda dedicado, por ejemplo:

    conda activate GEOF

Tambien puede usarse el instalador auxiliar Tkinter:

    python AUX_env_installer_ivoox_tk.py

Ese instalador diagnostica dependencias, estructura del proyecto y smoke tests antes de ejecutar acciones de instalacion.


Como ejecutar la aplicacion
---------------------------

Forma normal durante desarrollo:

    python run_gui.py

Desde Spyder/IPython:

    %runfile C:/ruta/al/proyecto/run_gui.py --wdir

Comportamiento esperado:

1. run_gui.py actua como bootstrap.
2. Lanza una instancia detached mediante pythonw.exe/schtasks.
3. Spyder o la terminal quedan libres.
4. Se abre la GUI principal en primer plano.
5. Aparece el icono de la aplicacion en la bandeja del sistema.
6. Si se cierra la ventana con X, la app sigue activa en tray.
7. Para cerrar realmente: clic derecho en el icono de tray -> Salir definitivamente.

Modo depuracion directa, sin desacoplar:

    python run_gui.py --debug-direct

Este modo es util para ver errores directamente en consola durante desarrollo.


Uso basico de la GUI
--------------------

1. Ingresar la URL de la pagina de episodios de iVoox.
   Ejemplo:

       https://www.ivoox.com/podcast-configuracion-vortice_sq_f11355472_1.html

2. Ajustar parametros:
   - Max. paginas.
   - Pausa entre paginas.
   - Workers paginas max.
   - Workers enrich max.
   - Escaneo paralelo por paginas.
   - Enriquecimiento paralelo.
   - Autoajustar workers segun equipo/latencia.

3. Presionar Escanear podcast.

4. Revisar la tabla de episodios.

5. Seleccionar carpeta de salida.

6. Ajustar nombres propuestos si se desea.

7. Marcar o desmarcar episodios.

8. Presionar Descargar episodios seleccionados.

9. Verificar en el log que la descarga y la portada se hayan procesado correctamente.


Logs principales
----------------

Los logs se guardan en la carpeta:

    logs/

Archivos relevantes:

- ivoox_launcher.log
  Registra el proceso bootstrap/detached.

- ivoox_child_boot.log
  Registra el arranque de la instancia real de la GUI.

- ivoox_gui.log
  Registra eventos principales de la aplicacion.

- last_ivoox_command.bat
  Guarda el ultimo comando reproducible usado para lanzar la app.

- run_IVOOX_PODCAST_DOWNLOADER_detached.cmd
  Script intermedio usado por schtasks para lanzar la app desacoplada.


Esquema ASCII del modulo
------------------------

IVOX_PODCAST_DOWNLOADER/
|
|-- run_gui.py
|   |-- Entry point unico de la aplicacion.
|   |-- Lanza la GUI en modo detached por defecto.
|   |-- Configura icono, AppUserModelID, Qt/plugins y logs de arranque.
|
|-- ivoox_daemon.py
|   |-- Utilidades Windows para lanzamiento detached.
|   |-- Maneja pythonw.exe, schtasks, marker de proceso, PID file y toast inicial.
|   |-- Reconstruye contexto de entorno para que Qt/PySide6 encuentre DLLs/plugins.
|
|-- AUX_env_installer_ivoox_tk.py
|   |-- Instalador/diagnosticador liviano en Tkinter.
|   |-- Revisa dependencias, estructura y smoke tests.
|   |-- Permite instalar acciones propuestas bajo confirmacion explicita.
|
|-- assets/
|   |-- ivoox_downloader.ico
|   |   |-- Icono de ventana, barra de tareas y tray app.
|
|-- gui/
|   |
|   |-- main_window.py
|   |   |-- Ventana principal PySide6.
|   |   |-- Controles de escaneo, tabla, descarga, miniatura, progreso y log.
|   |   |-- Tray app, notificaciones y cierre a segundo plano.
|   |
|   |-- episodes_table.py
|   |   |-- Tabla de episodios.
|   |   |-- Seleccion multiple, mover filas, eliminar filas, sincronizar nombres.
|   |
|   |-- collapsible_box.py
|   |   |-- Widget reutilizable para secciones colapsables.
|   |
|   |-- thumbnail_loader.py
|   |   |-- Cargador auxiliar de miniaturas con cache/session.
|   |
|   |-- workers.py
|       |-- Workers Qt para descubrimiento/descarga.
|       |-- Base para migrar trabajo en background a QThread si se desea.
|
|-- kernel/
|   |
|   |-- episode_model.py
|   |   |-- Dataclass/modelo de episodio.
|   |
|   |-- ivox_discovery.py
|   |   |-- Descubrimiento de episodios.
|   |   |-- Escaneo paralelo de paginas.
|   |   |-- Enriquecimiento paralelo de episodios.
|   |   |-- Autoajuste de workers y logs de rendimiento.
|   |
|   |-- ivox_download.py
|   |   |-- Descarga de audios.
|   |   |-- Renombrado final.
|   |   |-- Descarga, normalizacion y embebido de portada ID3.
|   |
|   |-- naming_schemes.py
|   |   |-- Reglas de nombres propuestos.
|   |   |-- Slugs, prefijos, numeracion y extension.
|   |
|   |-- logging_utils.py
|       |-- Configuracion de logger para archivo y GUI.
|
|-- logs/
|   |-- ivoox_launcher.log
|   |-- ivoox_child_boot.log
|   |-- ivoox_gui.log
|   |-- last_ivoox_command.bat
|   |-- run_IVOOX_PODCAST_DOWNLOADER_detached.cmd
|
|-- downloads/ o carpeta de salida definida por usuario
    |-- Archivos MP3 descargados con nombres propuestos.


Notas de desarrollo
-------------------

- run_gui.py es el punto de entrada oficial.
- Para depurar errores de GUI, usar --debug-direct.
- Para uso normal, ejecutar run_gui.py sin argumentos.
- La app esta preparada para tray app en Windows.
- El empaquetado futuro como .exe deberia considerar modo frozen mediante sys.frozen/sys.executable.
- En modo .exe, los usuarios finales no deberian necesitar Conda ni Python instalado.

