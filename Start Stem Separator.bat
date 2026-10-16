@echo off
rem Double-click to open the YARG Stem Separator in your browser.
rem First run sets up Python and the packages it needs (a few GB, needs uv: https://docs.astral.sh/uv/).
setlocal
cd /d "%~dp0"
title YARG Stem Separator

if not exist ".venv\Scripts\python.exe" (
    call :setup
    if errorlevel 1 goto :failed
)
if not exist ".venv\Scripts\ffmpeg.exe" call :ffmpeg

".venv\Scripts\python.exe" review.py --open %*
if errorlevel 1 goto :failed
goto :eof

:setup
where uv >nul 2>nul
if errorlevel 1 (
    echo uv is needed for the first-time setup but was not found.
    echo Install it from https://docs.astral.sh/uv/getting-started/installation/ and run this again.
    exit /b 1
)
echo First run: setting up Python and the separation packages.
echo This downloads a few GB and takes several minutes. It only happens once.
echo.
uv venv --python 3.12 .venv || exit /b 1
uv pip install --python .venv\Scripts\python.exe torch torchaudio --index-url https://download.pytorch.org/whl/cu128 || exit /b 1
uv pip install --python .venv\Scripts\python.exe "audio-separator[gpu]" soundfile imageio-ffmpeg mido audioread || exit /b 1
call :ffmpeg
echo.
echo Setup complete.
exit /b 0

:ffmpeg
for %%f in (".venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-*.exe") do copy /y "%%~f" ".venv\Scripts\ffmpeg.exe" >nul
exit /b 0

:failed
echo.
echo Something went wrong - see the messages above.
pause
exit /b 1
