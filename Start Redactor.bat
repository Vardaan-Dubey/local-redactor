@echo off
rem Local Redactor - Windows: double-click to set up (first time) and open the editor.
rem Installs everything into this folder (+ uv into %USERPROFILE%\.local\bin). No admin rights.
setlocal
cd /d "%~dp0"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
set "PY=.venv\Scripts\python.exe"
if not defined WHISPER_MODEL set "WHISPER_MODEL=small"

set "NEEDSETUP="
if not exist "%PY%" set "NEEDSETUP=1"
if not exist ".venv\.installed-requirements" set "NEEDSETUP=1"
if not defined NEEDSETUP (
  fc /b requirements.txt .venv\.installed-requirements >nul 2>nul || set "NEEDSETUP=1"
)

if defined NEEDSETUP (
  echo ==^> First-time setup ^(needs internet once; a few minutes^)
  where uv >nul 2>nul || (
    echo ==^> Installing uv ^(Python installer^) into %USERPROFILE%\.local\bin
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:UV_NO_MODIFY_PATH=1; irm https://astral.sh/uv/install.ps1 | iex" || goto :fail
  )
  echo ==^> Creating private Python 3.12 environment in .venv
  uv venv --python 3.12 --allow-existing .venv || goto :fail
  uv pip install --python "%PY%" -r requirements.txt || goto :fail
  copy /y requirements.txt .venv\.installed-requirements >nul
)

"%PY%" -m redactor setup --whisper-model %WHISPER_MODEL% || goto :fail

set "HF_HUB_OFFLINE=1"
set "PYTHONPATH=%CD%"
"%PY%" -m redactor %*
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo Something went wrong (see above).
pause
exit /b 1
