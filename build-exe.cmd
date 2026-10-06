@echo off
REM Build CSClipEncoder.exe, the window, from clipgui.py and clipenc.py.
REM
REM PyInstaller is the only third-party dependency, and it is a BUILD
REM dependency only: nothing that runs needs it. Install it with:
REM     python -m pip install pyinstaller
REM
REM --onefile      one .exe, nothing to install
REM --windowed     no console window behind the app
REM --clean        do not reuse a stale build cache after a code change
REM
REM The .exe is copied next to config.json, which is where it looks for its
REM settings and where it is started from. Every failure pauses, so a build
REM that never ran cannot be mistaken for one that did.

setlocal
cd /d "%~dp0"

REM Pick the Python that can BUILD: one with both PyInstaller and tkinter.
REM CLIPENC_PYTHON wins outright if it is set and runs at all.
set "PY="
set "PYANY="
call :override "%CLIPENC_PYTHON%"
call :try "python"
call :try "py"
call :try "C:\Program Files\Python313\python.exe"
call :try "C:\Program Files\Python312\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"

if not defined PY if defined PYANY set "PY=%PYANY%"

REM The messages live under labels at the end, outside any bracketed block:
REM a Python path with a bracket in it would otherwise end the block early.
if not defined PY goto :no_python
"%PY%" -c "import tkinter" >nul 2>&1
if errorlevel 1 goto :no_tkinter
"%PY%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 goto :no_pyinstaller

REM The paths to the icon and the assets folder are given in full. With
REM --specpath, PyInstaller reads relative ones from the build folder.
echo Building with "%PY%" ...
"%PY%" -m PyInstaller --onefile --windowed --clean ^
    --name CSClipEncoder ^
    --icon "%~dp0assets\icon.ico" ^
    --add-data "%~dp0assets;assets" ^
    --distpath dist --workpath build --specpath build ^
    clipgui.py
if errorlevel 1 (
    echo.
    echo BUILD FAILED. The PyInstaller output above says why.
    pause
    exit /b 1
)

echo.

REM Windows will not overwrite a running program.
copy /y "dist\CSClipEncoder.exe" "CSClipEncoder.exe" >nul
if errorlevel 1 (
    echo Built dist\CSClipEncoder.exe, but could not replace CSClipEncoder.exe.
    echo Close the CS Clip Encoder window if it is open, then run this again.
    echo Nothing is lost. The new build is in dist\.
    pause
    exit /b 1
)

REM Prove it actually rebuilt: the .exe must be newer than both source files.
REM Written without the less-than and greater-than signs, which cmd would
REM take for redirection.
"%PY%" -c "import os,sys;e=os.path.getmtime('CSClipEncoder.exe');s=max(os.path.getmtime('clipgui.py'),os.path.getmtime('clipenc.py'));sys.exit(0 if max(e,s)==e else 1)"
if errorlevel 1 (
    echo.
    echo WARNING: CSClipEncoder.exe is OLDER than its source, so the build did
    echo not take. Close any running CS Clip Encoder window and run this again.
    pause
    exit /b 1
)

for %%F in (CSClipEncoder.exe) do echo Built CSClipEncoder.exe  ^(%%~zF bytes, %%~tF^)
echo Double-click CSClipEncoder.exe to start it.
echo The window's title bar shows that build time, so you can tell which
echo build you are looking at.
pause
exit /b 0

:no_python
echo No usable Python found. Install python.org Python, or set
echo CLIPENC_PYTHON to a working python.exe.
pause
exit /b 9

:no_tkinter
echo.
echo "%PY%" has no tkinter, which the window is made of.
echo Install python.org Python with "tcl/tk and IDLE" ticked, or set
echo CLIPENC_PYTHON to a python.exe that has it.
echo.
echo Nothing was built.
pause
exit /b 9

:no_pyinstaller
echo.
echo PyInstaller is not installed for any Python found, including "%PY%".
echo Install it with:
echo     "%PY%" -m pip install pyinstaller
echo.
echo If that stops on a permissions error, this does the same without one:
echo     "%PY%" -m pip install --user pyinstaller
echo.
echo Nothing was built. CSClipEncoder.exe is untouched.
pause
exit /b 9

:try
REM A candidate qualifies only if it can import PyInstaller and tkinter. Any
REM candidate that merely starts is remembered in PYANY, so a total miss can
REM still be reported against a real Python.
if defined PY exit /b 0
if "%~1"=="" exit /b 0
"%~1" -c "import sys" >nul 2>&1
if errorlevel 1 exit /b 0
if not defined PYANY set "PYANY=%~1"
"%~1" -c "import PyInstaller, tkinter" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
exit /b 0

:override
REM CLIPENC_PYTHON, used if it runs at all, PyInstaller or not.
if "%~1"=="" exit /b 0
"%~1" -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PY=%~1"
    set "PYANY=%~1"
)
exit /b 0
