@echo off
REM Install pgvector 0.8.7 into the local PostgreSQL 18 installation.
REM Must be run as Administrator (right click -> Run as administrator).
REM
REM This script only copies extension files. Creating the database and
REM running CREATE EXTENSION is done afterwards from the app side, because
REM that needs the postgres password and should not be hardcoded here.
REM
REM All output is ASCII on purpose: cmd.exe under a Chinese code page
REM garbles UTF-8, and a half-readable installer is worse than none.

setlocal
set "SRC=%~dp0pgvector-pg18"
set "DST=D:\Program Files\PostgreSQL\18"
set "PGBIN=%DST%\bin"

echo.
echo  pgvector 0.8.7 installer for PostgreSQL 18
echo  ------------------------------------------------------------
echo  source : %SRC%
echo  target : %DST%
echo.

if not exist "%SRC%\lib\vector.dll" (
  echo [ERROR] vector.dll not found under %SRC%
  goto :end
)
if not exist "%DST%\bin\pg_ctl.exe" (
  echo [ERROR] PostgreSQL 18 not found at %DST%
  echo         Edit DST in this file if your install path differs.
  goto :end
)

net session >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Not running as Administrator.
  echo         Right click this file and choose "Run as administrator".
  goto :end
)

echo [1/2] Copying extension files ...
robocopy "%SRC%" "%DST%" /E /NFL /NDL /NP /R:1 /W:1
if errorlevel 8 (
  echo [ERROR] Copy failed.
  goto :end
)

echo [2/2] Verifying ...
if exist "%DST%\lib\vector.dll" (
  echo       OK  vector.dll
) else (
  echo       MISSING  vector.dll
)
if exist "%DST%\share\extension\vector.control" (
  echo       OK  vector.control
) else (
  echo       MISSING  vector.control
)

echo.
echo  Extension files installed.
echo  Next: restart is NOT required for the files to be picked up,
echo  but CREATE EXTENSION must run once per database.
echo.

:end
echo Press any key to close this window.
pause >nul
