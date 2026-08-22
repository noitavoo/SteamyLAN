@echo off
setlocal EnableExtensions
cd /d "%~dp0"


echo SteamyLAN Windows build
echo =====================================
echo.

set "DLL_SOURCE="

if defined STEAM_API64_DLL if exist "%STEAM_API64_DLL%" set "DLL_SOURCE=%STEAM_API64_DLL%"

if not defined DLL_SOURCE if exist "%~dp0steam_api64.dll" set "DLL_SOURCE=%~dp0steam_api64.dll"
if not defined DLL_SOURCE if exist "%~dp0SteamyLan\steam_api64.dll" set "DLL_SOURCE=%~dp0SteamyLan\steam_api64.dll"
if not defined DLL_SOURCE if exist "%~dp0sdk\redistributable_bin\win64\steam_api64.dll" set "DLL_SOURCE=%~dp0sdk\redistributable_bin\win64\steam_api64.dll"
if not defined DLL_SOURCE if exist "%~dp0steamworks_sdk\sdk\redistributable_bin\win64\steam_api64.dll" set "DLL_SOURCE=%~dp0steamworks_sdk\sdk\redistributable_bin\win64\steam_api64.dll"

if not defined DLL_SOURCE if defined STEAMWORKS_SDK if exist "%STEAMWORKS_SDK%\redistributable_bin\win64\steam_api64.dll" set "DLL_SOURCE=%STEAMWORKS_SDK%\redistributable_bin\win64\steam_api64.dll"
if not defined DLL_SOURCE if defined STEAMWORKS_SDK if exist "%STEAMWORKS_SDK%\sdk\redistributable_bin\win64\steam_api64.dll" set "DLL_SOURCE=%STEAMWORKS_SDK%\sdk\redistributable_bin\win64\steam_api64.dll"

if not defined DLL_SOURCE goto :dll_missing
if not exist "third_party\windivert\x64\WinDivert.dll" goto :windivert_missing
if not exist "third_party\windivert\x64\WinDivert64.sys" goto :windivert_missing

echo Steam DLL: "%DLL_SOURCE%"
echo.

python -c "import sys,struct; raise SystemExit(0 if sys.version_info[:2] == (3,14) and sys.version_info.micro >= 7 and struct.calcsize('P') == 8 else 1)"
if errorlevel 1 goto :versionfail

python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 goto :pyinstaller_missing

python -c "import psutil" >nul 2>nul
if errorlevel 1 goto :psutil_missing

echo [1/3] Building SteamyLAN...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul

python -m PyInstaller --noconfirm --clean --windowed --onedir --name SteamyLAN --icon SteamyLan\steamylan.ico --add-data="SteamyLan\steamylan.ico:SteamyLan" --add-data="SteamyLan\steamylan.png:SteamyLan" --add-data="third_party\windivert\LICENSE.txt:third_party\windivert" --add-data="third_party\windivert\NOTICE.md:third_party\windivert" --add-data="third_party\windivert\x64\WinDivert64.sys:." --add-binary="%DLL_SOURCE%:." --add-binary="third_party\windivert\x64\WinDivert.dll:." run.py
if errorlevel 1 goto :build_failed

if not exist "dist\SteamyLAN\SteamyLAN.exe" goto :build_missing
copy /Y "third_party\windivert\x64\WinDivert.dll" "dist\SteamyLAN\WinDivert.dll" >nul
if errorlevel 1 goto :windivert_copy_failed
copy /Y "third_party\windivert\x64\WinDivert64.sys" "dist\SteamyLAN\WinDivert64.sys" >nul
if errorlevel 1 goto :windivert_copy_failed

python -m PyInstaller --noconfirm --clean --onefile --windowed --name SteamyLANUpdate --icon SteamyLan\steamylan.ico tools\update_helper.py
if errorlevel 1 goto :updater_build_failed
if not exist "dist\SteamyLANUpdate.exe" goto :updater_build_missing
copy /Y "dist\SteamyLANUpdate.exe" "dist\SteamyLAN\SteamyLANUpdate.exe" >nul
if errorlevel 1 goto :updater_copy_failed

echo.
echo [2/3] Trimming Windows build...
rmdir /s /q "dist\SteamyLAN_trimmed" 2>nul
del /q "dist\SteamyLAN_usage.json" 2>nul

python tools\trim_windows_build.py --build "dist\SteamyLAN" --exe "SteamyLAN.exe" --output "dist\SteamyLAN_trimmed" --manifest "dist\SteamyLAN_usage.json"
if errorlevel 1 goto :trim_failed

if not exist "dist\SteamyLAN_trimmed\SteamyLAN.exe" goto :trim_output_missing

echo.
echo [3/3] Copying steam_api64.dll...
copy /Y "%DLL_SOURCE%" "dist\SteamyLAN\steam_api64.dll" >nul
if errorlevel 1 goto :dll_copy_failed

copy /Y "%DLL_SOURCE%" "dist\SteamyLAN_trimmed\steam_api64.dll" >nul
if errorlevel 1 goto :dll_copy_failed

copy /Y "dist\SteamyLANUpdate.exe" "dist\SteamyLAN_trimmed\SteamyLANUpdate.exe" >nul
if errorlevel 1 goto :updater_copy_failed

copy /Y "third_party\windivert\x64\WinDivert.dll" "dist\SteamyLAN_trimmed\WinDivert.dll" >nul
if errorlevel 1 goto :windivert_copy_failed
copy /Y "third_party\windivert\x64\WinDivert64.sys" "dist\SteamyLAN_trimmed\WinDivert64.sys" >nul
if errorlevel 1 goto :windivert_copy_failed

copy /Y "%DLL_SOURCE%" "dist\steam_api64.dll" >nul
if errorlevel 1 goto :dll_copy_failed

echo.
echo =====================================
echo Build complete.
echo.
echo Full build:    dist\SteamyLAN\
echo Trimmed build: dist\SteamyLAN_trimmed\
echo Steam DLL copied into dist and beside both SteamyLAN.exe files.
echo =====================================
echo.
pause
exit /b 0

:dll_missing
echo.
echo ERROR: steam_api64.dll was not found.
echo.
echo Put the official 64-bit Steamworks DLL in one of these locations:
echo   %~dp0steam_api64.dll
echo   %~dp0SteamyLan\steam_api64.dll
echo   %~dp0sdk\redistributable_bin\win64\steam_api64.dll
echo   %~dp0steamworks_sdk\sdk\redistributable_bin\win64\steam_api64.dll
echo.
echo Or set STEAM_API64_DLL to its full path before running this script.
echo Nothing was built.
pause
exit /b 1

:windivert_missing
echo.
echo ERROR: The official WinDivert x64 runtime is missing.
echo Expected third_party\windivert\x64\WinDivert.dll and WinDivert64.sys.
echo Nothing was built.
pause
exit /b 1

:versionfail
echo.
echo ERROR: SteamyLAN requires 64-bit Python 3.14.7 or newer within the 3.14 series.
pause
exit /b 1

:pyinstaller_missing
echo.
echo ERROR: PyInstaller is not available in the Python environment on PATH.
echo Nothing was installed or changed.
pause
exit /b 1

:psutil_missing
echo.
echo ERROR: psutil is not available in the Python environment on PATH.
echo SteamyLAN already uses psutil at runtime; this script does not install anything.
pause
exit /b 1

:build_failed
echo.
echo ERROR: Build failed.
pause
exit /b 1

:build_missing
echo.
echo ERROR: PyInstaller finished but dist\SteamyLAN\SteamyLAN.exe was not found.
pause
exit /b 1

:updater_build_failed
echo.
echo ERROR: The updater helper build failed.
pause
exit /b 1

:updater_build_missing
echo.
echo ERROR: PyInstaller did not create SteamyLANUpdate.exe.
pause
exit /b 1

:updater_copy_failed
echo.
echo ERROR: Failed to copy SteamyLANUpdate.exe into the application build.
pause
exit /b 1

:windivert_copy_failed
echo.
echo ERROR: Failed to copy the WinDivert runtime into the application build.
pause
exit /b 1

:trim_failed
echo.
echo ERROR: The trim operation did not finish successfully.
pause
exit /b 1

:trim_output_missing
echo.
echo ERROR: Trim finished but dist\SteamyLAN_trimmed\SteamyLAN.exe was not found.
pause
exit /b 1

:dll_copy_failed
echo.
echo ERROR: Failed to copy steam_api64.dll into the build output.
pause
exit /b 1
