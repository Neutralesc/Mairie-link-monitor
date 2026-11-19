@echo off
rem Double-cliquez ici pour démarrer l'application web, puis ouvrez http://localhost:8080 dans le navigateur.
cd /d "%~dp0"
py -3 src\mairie_link_monitor.py web
pause
