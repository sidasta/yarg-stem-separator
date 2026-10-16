@echo off
rem Double-click to open the YARG Stem Separator in your browser.
rem First run sets up Python and the packages it needs (a few GB). If uv isn't installed,
rem a private copy is downloaded into the "tools" folder; nothing is installed system-wide.
setlocal
cd /d "%~dp0"
title YARG Stem Separator

rem A setup that no longer starts (e.g. the folder was moved) is rebuilt from the download cache.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "" >nul 2>nul
    if errorlevel 1 (
        echo The Python setup in this folder no longer works; rebuilding it...
        rmdir /s /q ".venv"
    )
)
if not exist ".venv\Scripts\python.exe" (
    call :setup
    if errorlevel 1 goto :failed
)
if not exist ".venv\Scripts\ffmpeg.exe" call :ffmpeg

".venv\Scripts\python.exe" review.py --open %*
if errorlevel 1 goto :failed
goto :eof

:setup
call :find_uv
if errorlevel 1 exit /b 1
echo Setting up Python and the separation packages.
echo The first time, this downloads a few GB and takes several minutes.
echo.
"%UV%" venv --python 3.12 .venv || exit /b 1
"%UV%" pip install --python .venv\Scripts\python.exe torch torchaudio --index-url https://download.pytorch.org/whl/cu128 || exit /b 1
"%UV%" pip install --python .venv\Scripts\python.exe "audio-separator[gpu]" soundfile imageio-ffmpeg mido audioread || exit /b 1
call :ffmpeg
echo.
echo Setup complete.
exit /b 0

:find_uv
where uv >nul 2>nul
if not errorlevel 1 (
    set "UV=uv"
    exit /b 0
)
rem No uv installed: use a private copy, and keep everything it downloads inside this folder.
set "UV=%~dp0tools\uv.exe"
set "UV_CACHE_DIR=%~dp0tools\uv-cache"
set "UV_PYTHON_INSTALL_DIR=%~dp0tools\python"
set "UV_PYTHON_PREFERENCE=only-managed"
if exist "%UV%" exit /b 0
echo Downloading uv (the tool that installs Python for this app) into the tools folder...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\get-uv.ps1"
if not exist "%UV%" (
    echo Could not download uv. You can install it yourself from
    echo https://docs.astral.sh/uv/getting-started/installation/ and run this again.
    exit /b 1
)
exit /b 0

:ffmpeg
for %%f in (".venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-*.exe") do copy /y "%%~f" ".venv\Scripts\ffmpeg.exe" >nul
exit /b 0

:failed
echo.
echo Something went wrong - see the messages above.
pause
exit /b 1
