@echo off
rem VidAuto stopper. Keep this file ASCII-only: cmd.exe misreads UTF-8 batch files.
title VidAuto - stop
cd /d "%~dp0"
echo.
echo  Stopping n8n and VidAuto...
docker compose down
echo.
echo  Stopped. Your videos and n8n settings are kept.
echo  To start again run the start file in this folder.
timeout /t 10 >nul
