@echo off
rem Pantalla del simulador de pools en http://localhost:8768 (etapas\simulador_web.py). La lanza la tarea de Windows "Simulador web" al iniciar sesion y queda corriendo en segundo plano.
rem Si el puerto ya esta en uso (otro simulador abierto), Python falla al arrancar y no pasa nada.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist out mkdir out
echo %date% %time% arranque >> out\simulador_web.log
"C:\Users\javie\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe" -m etapas.simulador_web >> out\simulador_web.log 2>&1
