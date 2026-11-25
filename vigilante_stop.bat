@echo off
rem Vigilante del stop de tus posiciones de liquidez (etapas\vigilante_stop.py). Lo lanza la tarea de Windows "Vigilante stop" cada hora.
rem El token de GitHub se saca aqui: el Python de Microsoft Store no deja a gh leerlo del Administrador de credenciales.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
for /f "delims=" %%t in ('gh auth token') do set GH_TOKEN=%%t
"C:\Users\javie\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe" -m etapas.vigilante_stop >> out\vigilante_stop.log 2>&1
