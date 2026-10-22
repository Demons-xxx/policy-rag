@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 goto nopython
python start.py %*
set "CODE=%errorlevel%"
echo.
echo [PolicyRAG] exit code %CODE% - server runs in background, you can close this window.
pause
exit /b %CODE%

:nopython
echo.
echo [PolicyRAG] Python not found.
echo Install Python 3.10+ and tick "Add python.exe to PATH".
pause
exit /b 1
