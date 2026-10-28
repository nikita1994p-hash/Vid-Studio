@echo off
rem VidAuto launcher. Keep this file ASCII-only: cmd.exe misreads labels in UTF-8 batch files.
title VidAuto - start
cd /d "%~dp0"

echo.
echo  ===  VidAuto: starting  ===
echo.

rem 1. Docker Desktop: start it if the engine is not answering yet
docker info >nul 2>&1
if not errorlevel 1 goto docker_running
echo  [1/3] Starting Docker Desktop...
if not exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" goto no_docker
start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
set /a tries=0
:wait_docker
timeout /t 5 /nobreak >nul
docker info >nul 2>&1
if not errorlevel 1 goto docker_ready
set /a tries+=1
if %tries% lss 48 goto wait_docker
echo  Docker Desktop did not start within 4 minutes. Open it by hand, then run this file again.
pause
exit /b 1
:no_docker
echo  Docker Desktop is not installed. Install it from docker.com, then run this file again.
pause
exit /b 1
:docker_running
echo  [1/3] Docker Desktop is already running.
:docker_ready

rem 2. Containers
echo  [2/3] Starting n8n and VidAuto (the very first time takes 3-10 minutes)...
docker compose up -d
if errorlevel 1 goto compose_failed

rem 3. Wait for the dashboard and open it
echo  [3/3] Waiting for the dashboard...
set /a tries=0
:wait_dash
curl -s -o nul http://localhost:8000/api/health && goto dash_ready
set /a tries+=1
if %tries% geq 30 goto dash_slow
timeout /t 2 /nobreak >nul
goto wait_dash
:dash_slow
echo  The dashboard is slow to answer. Open it by hand: http://localhost:8000
goto done
:dash_ready
start "" http://localhost:8000
:done
echo.
echo  Ready!
echo    Dashboard:  http://localhost:8000
echo    n8n:        http://localhost:5678
echo.
echo  You can close this window. To stop everything run the stop file in this folder.
timeout /t 15 >nul
exit /b 0

:compose_failed
echo.
echo  Could not start the containers. Copy the error text above and send it.
pause
exit /b 1
