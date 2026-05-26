@echo off
REM Argos - aggiornamento client (wrapper per scripts\update_client.ps1).
REM Doppio-click su questo file per aggiornare, oppure: .\update.bat da PowerShell.
REM
REM Perche' un .bat invece di lanciare direttamente lo .ps1:
REM la ExecutionPolicy di default su Windows blocca gli script .ps1 non firmati.
REM Questo .bat passa -ExecutionPolicy Bypass solo al processo che parte qui,
REM senza modificare le policy del sistema.
REM
REM Cosa fa:
REM  1. Si sposta nella cartella di questo .bat (funziona da qualsiasi shortcut)
REM  2. Verifica che lo script PS1 esista
REM  3. Lo lancia con bypass dell'execution policy
REM  4. Resta aperto a fine sessione cosi' puoi leggere l'output

cd /d "%~dp0"

if not exist "scripts\update_client.ps1" (
    echo.
    echo [ERROR] scripts\update_client.ps1 non trovato.
    echo Sei sicuro di essere nella cartella di Argos?
    echo.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\update_client.ps1"
set "EXITCODE=%ERRORLEVEL%"

echo.
echo ============================================================
if "%EXITCODE%"=="0" (
    echo Update completato. Premi un tasto per chiudere la finestra.
) else (
    echo Update terminato con errori ^(exit %EXITCODE%^). Leggi i messaggi qui sopra.
)
echo ============================================================
pause >nul
exit /b %EXITCODE%
