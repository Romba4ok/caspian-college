@echo off
setlocal
chcp 65001 >nul
title Caspian College - Live Monitor
python -X utf8 "%~dp0scripts\request_monitor.py" %*
if errorlevel 1 pause
