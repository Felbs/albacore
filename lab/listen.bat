@echo off
rem albacore live listening — the human test button.
rem usage: listen.bat [MHz] [program]   (default 93.3 HD1)
rem Decodes through the albacore build with ALBACORE=1 (certified stack).
set "NRSC5_EXE=Z:\src\albacore\build\src\nrsc5.exe"
set "ALBACORE=1"
set "ALBACORE_COSTAS_BW=auto"
rem antenna defaults: port C (discone) while the A/B RFI storm persists
if not defined HD_ANT set "HD_ANT=Antenna C"
if not defined HD_IFGR set "HD_IFGR=30"
if not defined HD_RFGAIN set "HD_RFGAIN=7"
set "PATH=C:\msys64\mingw64\bin;%PATH%"
set "MHZ=%~1"
if "%MHZ%"=="" set "MHZ=93.3"
set "PROG=%~2"
if "%PROG%"=="" set "PROG=0"
echo === albacore live: %MHZ% MHz program %PROG% (ALBACORE=1) ===
echo close this window (or Ctrl+C) to stop and release the SDR
if not defined RADIOCONDA_PY set "RADIOCONDA_PY=%USERPROFILE%\radioconda\python.exe"
"%RADIOCONDA_PY%" "%~dp0hd_listen.py" --mhz %MHZ% --prog %PROG%
pause
