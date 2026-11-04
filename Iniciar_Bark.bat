@echo off
setlocal
cd /d "%~dp0"
where pyw >nul 2>nul
if not errorlevel 1 (
    start "" /b pyw -3 -B iniciar_bark.pyw
    exit /b
)
start "" /b pythonw -B iniciar_bark.pyw
endlocal
