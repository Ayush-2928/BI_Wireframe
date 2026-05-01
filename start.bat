@echo off
title BI Wireframe Agent
echo ================================================
echo   BI Wireframe Agent
echo ================================================
echo   API:   http://localhost:8000
echo   Ngrok: https://prism-debtor-student.ngrok-free.dev
echo   Docs:  https://prism-debtor-student.ngrok-free.dev/docs
echo ================================================
echo.

:: Start ngrok in a separate background window
start "ngrok" cmd /k "ngrok http 8000 --domain=prism-debtor-student.ngrok-free.dev"

:: Run the server in THIS window so logs are visible here
cd /d %~dp0
python main.py
