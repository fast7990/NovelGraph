@echo off
REM ASCII only: avoid code page garbling under cmd.exe.
setlocal
set "SRC=D:\project\jiansuoandzhishitupi\tools\pgvector-pg18"
set "DST=D:\Program Files\PostgreSQL\18"
set "LOG=D:\project\jiansuoandzhishitupi\tools\pgvector-install.log"

echo === pgvector install %DATE% %TIME% === > "%LOG%"
net session >nul 2>&1
if errorlevel 1 (
  echo [ERROR] not elevated >> "%LOG%"
  exit /b 2
)
robocopy "%SRC%" "%DST%" /E /NFL /NDL /NP /R:1 /W:1 >> "%LOG%" 2>&1
if errorlevel 8 (
  echo [ERROR] robocopy failed >> "%LOG%"
  exit /b 3
)
if exist "%DST%\lib\vector.dll" (echo OK vector.dll >> "%LOG%") else (echo MISSING vector.dll >> "%LOG%")
if exist "%DST%\share\extension\vector.control" (echo OK vector.control >> "%LOG%") else (echo MISSING vector.control >> "%LOG%")
echo DONE >> "%LOG%"
exit /b 0
