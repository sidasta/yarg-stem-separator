@echo off
rem Double-click to open the YARG Stem Separator in your browser.
rem First run sets up Python and the packages for your graphics card (NVIDIA, AMD or CPU-only).
rem If uv isn't installed, a private copy is downloaded into the "tools" folder; nothing is
rem installed system-wide. Set STEMSEP_DEVICE=nvidia, amd or cpu to override the hardware choice.
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
rem (Labels and goto rather than bracketed blocks: %UV% inside a block would be read
rem before :find_uv sets it.)
if not exist ".venv\Scripts\python.exe" goto :make_venv
:venv_ready
rem The setup script writes this marker only after everything installed successfully.
if not exist ".venv\stemsep-backend.json" goto :install
:start
".venv\Scripts\python.exe" review.py --open %*
if errorlevel 1 goto :failed
goto :eof

:make_venv
call :find_uv
if errorlevel 1 goto :failed
echo Setting up Python. The first time, this downloads a few GB and takes several minutes.
"%UV%" venv --python 3.12 .venv
if errorlevel 1 goto :failed
goto :venv_ready

:install
call :find_uv
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" scripts\setup_env.py
if errorlevel 1 goto :failed
echo.
echo Setup complete.
goto :start

:find_uv
if defined UV exit /b 0
where uv >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%u in ('where uv') do if not defined UV set "UV=%%u"
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

:failed
echo.
echo Something went wrong - see the messages above.
pause
exit /b 1
