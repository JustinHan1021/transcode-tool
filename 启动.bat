@echo off
rem Launcher for the transcoding tool.
rem You can also drag media files (or folders) onto this file to import them.
setlocal
cd /d "%~dp0"

rem Locate the entry script by extension so the file stays ASCII-only.
set "ENTRY="
for %%F in ("*.pyw") do if not defined ENTRY set "ENTRY=%%~fF"
if not defined ENTRY set "ENTRY=%~dp0transcode.pyw"

rem Prefer the windowed interpreter: no console window left behind.
rem %%~$PATH:P expands to a full path when the program is on PATH, and to
rem nothing when it is not; 'set VAR=' then undefines the variable.
set "PY="
for %%P in (pythonw.exe) do set "PY=%%~$PATH:P"
if not defined PY for %%P in (pyw.exe) do set "PY=%%~$PATH:P"

if defined PY goto :launch_windowed

rem No windowed interpreter: use the console one so errors stay readable.
set "PY="
for %%P in (python.exe) do set "PY=%%~$PATH:P"
if not defined PY for %%P in (py.exe) do set "PY=%%~$PATH:P"
if defined PY goto :launch_console

echo.
echo   Python was not found on this computer.
echo.
echo   Please install Python 3.9 or newer:
echo       https://www.python.org/downloads/
echo   During setup, tick "Add python.exe to PATH".
echo.
pause
exit /b 1

:launch_windowed
start "" "%PY%" "%ENTRY%" %*
exit /b 0

:launch_console
echo   Starting with a console window so any error stays visible.
echo   Keep this window open while the program runs.
echo.
"%PY%" "%ENTRY%" %*
if errorlevel 1 (
  echo.
  echo   The program exited with an error. See the message above,
  echo   and check the file startup-error.log next to this script.
  pause
)
exit /b 0
