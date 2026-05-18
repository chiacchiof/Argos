@echo off
REM AgentScraper - avvio rapido dell'app.
REM Doppio-click su questo file per avviare il server (oppure: .\start.bat da PowerShell).
REM
REM Cosa fa:
REM  1. Si sposta nella cartella di questo .bat (funziona da qualsiasi shortcut)
REM  2. Verifica che la venv esista (altrimenti rimanda a install_client.ps1)
REM  3. Attiva la venv e lancia `agentscraper`
REM  4. Resta aperto a fine sessione cosi' puoi leggere eventuali errori

cd /d "%~dp0"

if not exist ".venv\Scripts\agentscraper.exe" (
    echo.
    echo [ERROR] .venv non trovata o installazione incompleta.
    echo.
    echo Esegui prima l'installer:
    echo     .\scripts\install_client.ps1
    echo.
    pause
    exit /b 1
)

echo ============================================================
echo   AgentScraper - avvio in corso
echo ============================================================
echo.
echo Quando vedi "Uvicorn running on http://127.0.0.1:8000",
echo apri quell'indirizzo nel browser e fai login.
echo.
echo Per fermare il server premi Ctrl+C in questa finestra.
echo ============================================================
echo.

call ".venv\Scripts\activate.bat"
agentscraper

echo.
echo ============================================================
echo Server terminato. Premi un tasto per chiudere la finestra.
echo ============================================================
pause >nul
