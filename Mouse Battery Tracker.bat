@echo off
rem Double-click to start the tray app.
rem
rem Uses pythonw.exe rather than python.exe so no console window stays open.
rem "start" returns immediately, so this script exits at once rather than
rem lingering for the life of the app.
rem
rem The real interpreter is preferred over whatever "pythonw" resolves to on
rem PATH: on this machine that is a 0 KB App Execution Alias which launches the
rem real interpreter as a child and then sits there costing ~15 MB for nothing.
rem
rem Keep this file CRLF-terminated. cmd.exe mis-parses parenthesised blocks in
rem LF-only batch files.

cd /d "%~dp0"

set "PYW=%LOCALAPPDATA%\Python\pythoncore-3.14-64\pythonw.exe"

if exist "%PYW%" (
    start "" "%PYW%" -m mbt tray
) else (
    where pythonw >nul 2>&1
    if errorlevel 1 (
        echo Could not find pythonw.exe.
        echo Install Python, or edit PYW in this file to point at pythonw.exe.
        pause
    ) else (
        start "" pythonw -m mbt tray
    )
)
