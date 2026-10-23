@echo off
rem Aviso de factor de salud en Aave (etapas\aave_aviso.py). Lo lanza el Programador de tareas una vez al día (09:30) y al iniciar sesión.
rem El token de GitHub se saca aqui: el Python de Microsoft Store no deja a gh leerlo del Administrador de credenciales.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
for /f "delims=" %%t in ('gh auth token') do set GH_TOKEN=%%t
echo %date% %time% >> out\aave_aviso.log
"C:\Users\javie\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe" -m etapas.aave_aviso >> out\aave_aviso.log 2>&1
