@echo off
rem Lastbook, from this drive, on Windows (64-bit). Double-click it.
rem It opens the library in the LastbookData folder beside this file and keeps everything it writes on this drive.
rem Where this computer has Microsoft's WebView2 (Windows 11 has; most Windows 10 have) and the app was built with its
rem window, Lastbook opens in a window of its own. Otherwise it opens in the web browser, at an address only this
rem computer can reach.
setlocal
set "HERE=%~dp0"
set "PROGRAM=%HERE%bin\windows-x64\Lastbook\Lastbook.exe"
set "DATA=%HERE%LastbookData"

if /i not "%PROCESSOR_ARCHITECTURE%"=="AMD64" if /i not "%PROCESSOR_ARCHITEW6432%"=="AMD64" (
  echo This drive's Windows program is for 64-bit Intel and AMD computers. This one is %PROCESSOR_ARCHITECTURE%.
  pause
  exit /b 1
)
if not exist "%PROGRAM%" (
  echo The Windows program is not on this drive: %HERE%bin\windows-x64\Lastbook
  echo It is built by the fork's GitHub Actions ^(Lastbook-portable-windows-x64.zip^) and put here with packaging\portable\make_drive.py.
  pause
  exit /b 1
)
if not exist "%DATA%" mkdir "%DATA%"
if not exist "%DATA%\portable.json" (
  >"%DATA%\portable.json" echo {
  >>"%DATA%\portable.json" echo  "portable": true,
  >>"%DATA%\portable.json" echo  "layout": 1
  >>"%DATA%\portable.json" echo }
)
start "" "%PROGRAM%" --window-or-browser --data "%DATA%" --portable %*
endlocal
