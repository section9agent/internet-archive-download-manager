@echo off
REM Builds ArchiveDownloader.exe (standalone, no Python needed to run it).
REM Run this ONCE on a Windows PC that has Python installed.
REM Put this file in the same folder as archive_downloader.py.

setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install it from python.org first ^(tick "Add python.exe to PATH"^).
    pause
    exit /b 1
)

echo Installing PyInstaller...
python -m pip install --upgrade pyinstaller
if errorlevel 1 goto :fail

echo Building...
python -m PyInstaller --onefile --windowed --clean --name ArchiveDownloader archive_downloader.py
if errorlevel 1 goto :fail

echo.
echo Done! Your program is: %~dp0dist\ArchiveDownloader.exe
echo You can copy that single file anywhere and share it.
pause
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
pause
exit /b 1
