@echo off
setlocal
chcp 65001 >nul
title Caspian College - просмотр дизайна
python -X utf8 "%~dp0scripts\design_preview.py"
if errorlevel 1 pause
