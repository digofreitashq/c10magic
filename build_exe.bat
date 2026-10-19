@echo off
rem Gera dist\C10Magic.exe (precisa do venv com as dependencias e o PyInstaller).
setlocal
cd /d "%~dp0"

if not exist venv\Scripts\python.exe (
    echo Crie o ambiente primeiro: python -m venv venv ^&^& venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 1
)

venv\Scripts\python.exe -m pip install --quiet pyinstaller || exit /b 1
venv\Scripts\python.exe -m PyInstaller C10Magic.spec --noconfirm --clean || exit /b 1

echo.
echo Pronto: dist\C10Magic.exe
