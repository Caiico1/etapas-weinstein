@echo off
rem Calibracion mensual del simulador de pools (etapas\simulador_aviso.py). La lanza la tarea de Windows "Simulador calibracion" el dia 1 de cada mes (o al encender el PC si se paso).
rem El token de GitHub se saca aqui: el Python de Microsoft Store no deja a gh leerlo del Administrador de credenciales.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
for /f "delims=" %%t in ('gh auth token') do set GH_TOKEN=%%t
echo %date% %time% >> out\simulador_aviso.log
"C:\Users\javie\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe" -m etapas.simulador_aviso >> out\simulador_aviso.log 2>&1
