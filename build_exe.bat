@echo off
rem Gera dist\C10Magic\C10Magic.exe e dist\C10Magic.zip
rem (precisa do venv com as dependencias; o PyInstaller e instalado aqui).
setlocal
cd /d "%~dp0"

if not exist venv\Scripts\python.exe (
    echo Crie o ambiente primeiro: python -m venv venv ^&^& venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 1
)

venv\Scripts\python.exe -m pip install --quiet pyinstaller || exit /b 1
venv\Scripts\python.exe -m PyInstaller C10Magic.spec --noconfirm --clean || exit /b 1

rem Zip para distribuir (a pasta inteira precisa acompanhar o .exe).
rem tar (do Windows 10+) grava caminhos padrao; o Compress-Archive grava com barra invertida.
if exist dist\C10Magic.zip del dist\C10Magic.zip
"%SystemRoot%\System32\tar.exe" -a -c -f dist\C10Magic.zip -C dist C10Magic || exit /b 1

echo.
echo Pronto:
echo   dist\C10Magic\C10Magic.exe   (execute a partir da pasta)
echo   dist\C10Magic.zip            (para copiar para outro computador)
