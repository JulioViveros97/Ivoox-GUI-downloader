@echo off
REM 2026-05-04T02:47:10+00:00
REM Entorno reconstruido para pythonw.exe detached.
set "IVOOX_PYTHONW_EXE=C:\Users\Julio\anaconda3\envs\GEOF\pythonw.exe"
set "CONDA_PREFIX=C:\Users\Julio\anaconda3\envs\GEOF"
set "CONDA_DEFAULT_ENV=GEOF"
set "CONDA_DLL_SEARCH_MODIFICATION_ENABLE=1"
set "QT_PLUGIN_PATH=C:\Users\Julio\anaconda3\envs\GEOF\Library\plugins;C:\Users\Julio\anaconda3\envs\GEOF\Library\lib\qt6\plugins"
set "QT_QPA_PLATFORM_PLUGIN_PATH=C:\Users\Julio\anaconda3\envs\GEOF\Library\plugins\platforms;C:\Users\Julio\anaconda3\envs\GEOF\Library\lib\qt6\plugins\platforms"
set "IVOOX_QT_IMAGEFORMATS_PATHS=C:\Users\Julio\anaconda3\envs\GEOF\Library\plugins\imageformats;C:\Users\Julio\anaconda3\envs\GEOF\Library\lib\qt6\plugins\imageformats"
set "IVOOX_THUMB_DECODER=auto"
set "PATH=C:\Users\Julio\anaconda3\envs\GEOF;C:\Users\Julio\anaconda3\envs\GEOF\Library\bin;C:\Users\Julio\anaconda3\envs\GEOF\Scripts;C:\Users\Julio\anaconda3\envs\GEOF\Lib\site-packages\PySide6;C:\Users\Julio\anaconda3\envs\GEOF;C:\Users\Julio\anaconda3\envs\GEOF\Library\mingw-w64\bin;C:\Users\Julio\anaconda3\envs\GEOF\Library\usr\bin;C:\Users\Julio\anaconda3\envs\GEOF\Library\bin;C:\Users\Julio\anaconda3\envs\GEOF\Scripts;C:\Users\Julio\anaconda3\envs\GEOF\bin;C:\Users\Julio\anaconda3\condabin;C:\Users\Julio\anaconda3\envs\GEOF;C:\Users\Julio\anaconda3\envs\GEOF\Library\mingw-w64\bin;C:\Users\Julio\anaconda3\envs\GEOF\Library\usr\bin;C:\Users\Julio\anaconda3\envs\GEOF\Library\bin;C:\Users\Julio\anaconda3\envs\GEOF\Scripts;C:\Windows\system32;C:\Windows;C:\Windows\System32\Wbem;C:\Windows\System32\WindowsPowerShell\v1.0;C:\Windows\System32\OpenSSH;C:\Program Files\MATLAB\R2022b\runtime\win64;C:\Program Files\MATLAB\R2022b\bin;C:\Program Files\dotnet;C:\Users\Julio\AppData\Local\Microsoft\WindowsApps;C:\Users\Julio\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin;%PATH%"
cd /d "C:\Users\Julio\Downloads\IVOX_PODCAST_DOWNLOADER_V7"
"C:\Users\Julio\anaconda3\envs\GEOF\pythonw.exe" "C:\Users\Julio\Downloads\IVOX_PODCAST_DOWNLOADER_V7\run_gui.py" "--ivoox-detached-child" "--ivoox-marker" "IVOOX_PODCAST_DOWNLOADER"
