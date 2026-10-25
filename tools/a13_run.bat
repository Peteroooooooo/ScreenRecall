@echo off
rem A13 one-click check. Double-click, press Enter, then leave the PC alone for a few minutes.
rem It disconnects Wi-Fi for the offline check (reconnects right after), switches the primary
rem display scaling to 150 / 125 / 100 percent, and restores the original scaling at the end.
rem Keep this file ASCII with CRLF line endings: cmd reads .bat files in the OEM code page.
chcp 65001 >nul
cd /d "%~dp0.."
".venv\Scripts\python.exe" tools\a13_check.py --wifi-off --scales 100,125,150
echo.
pause
