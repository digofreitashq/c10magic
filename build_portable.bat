@echo off
rem Gera o pacote portatil (Python oficial embutido + projeto). Ver build_portable.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_portable.ps1"
