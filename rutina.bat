@echo off
rem Rutina diaria de etapas: analiza watchlist.txt y deja informes, indice e historial en out\
rem La lanza el Programador de tareas de Windows, pero tambien se puede ejecutar con doble clic.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
"C:\Users\javie\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe" -m etapas --rutina > out\ultima_ejecucion.txt 2>&1
