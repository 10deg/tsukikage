@echo off
setlocal EnableExtensions
rem Build the browser-service Windows executable and keep every generated file
rem under out\windows-service.  Run from anywhere; the script changes to the
rem repository root itself.

pushd "%~dp0.." || exit /b 1
set "OUT=out\windows-service"
set "DIST=%OUT%\dist"
set "KERNEL_NAMES=naif0012.tls de440s.bsp pck00010.tpc gm_de440.tpc moon_pa_de440_200625.bpc moon_de440_220930.tf earth_latest_high_prec.bpc"

set "PYTHON="
where py >nul 2>nul
if not errorlevel 1 set "PYTHON=py -3.12"
if not defined PYTHON (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON=python"
)
if not defined PYTHON (
  echo [error] Python 3.12 was not found. Install it from python.org first.
  popd
  exit /b 1
)

set "VENV=build\windows-service-venv"
if not exist "%VENV%\Scripts\python.exe" (
  echo [1/5] creating %VENV%
  %PYTHON% -m venv "%VENV%" || goto :fail
)
call "%VENV%\Scripts\activate.bat" || goto :fail

echo [2/5] installing the minimal runtime
python -m pip install --upgrade pip || goto :fail
rem Base dependencies are numpy + spiceypy.  Pillow is needed for labels and
rem TIFF/EXR decoding; PyInstaller builds the one-file bundle.  Deliberately no
rem PySide6, matplotlib, timezonefinder or imageio-ffmpeg.
python -m pip install -e . "Pillow>=9.0" "numpy==2.0.2" "pyinstaller>=6.11" || goto :fail

echo [3/5] building %DIST%\Tsukikage.exe
set "TSUKIKAGE_BUILD_DIR=%CD%\%OUT%"
python -m PyInstaller --noconfirm --clean ^
  --distpath "%DIST%" --workpath "%OUT%\build\pyinstaller" ^
  packaging\tsukikage.spec || goto :fail

if not exist "%DIST%\Tsukikage.exe" (
  echo [error] %DIST%\Tsukikage.exe was not produced.
  goto :fail
)

echo [4/5] checking the bundle and assembling kernels\
for %%E in (tif tiff exr) do (
  dir /s /b "%OUT%\build\pyinstaller\tsukikage\*.%%E" 2>nul | findstr . >nul && (
    echo [error] a .%%E file entered the exe bundle.
    goto :fail
  )
)
if not exist "%DIST%\kernels" mkdir "%DIST%\kernels"
for %%K in (%KERNEL_NAMES%) do (
  if not exist "kernels\%%K" (
    echo [error] missing source kernel kernels\%%K
    goto :fail
  )
  copy /Y "kernels\%%K" "%DIST%\kernels\%%K" >nul || goto :fail
)
copy /Y "service\KERNELS-README.txt" "%DIST%\kernels\README.txt" >nul || goto :fail
copy /Y "service\WINDOWS-README.txt" "%DIST%\README.txt" >nul || goto :fail

echo [5/5] hashing and zipping the distributable folder
certutil -hashfile "%DIST%\Tsukikage.exe" SHA256 | findstr /v /c:"hash of file" /c:"CertUtil" > "%DIST%\Tsukikage.exe.sha256"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Compress-Archive -Path '%DIST%\*' -DestinationPath '%OUT%\Tsukikage-windows-x64.zip' -Force" || goto :fail

echo.
echo built: %DIST%\Tsukikage.exe
for %%F in ("%DIST%\Tsukikage.exe") do echo exe size: %%~zF bytes
echo kernels: %DIST%\kernels
echo zip:     %OUT%\Tsukikage-windows-x64.zip
echo.
echo The kernels are in dist\kernels and are required at run time.
echo Users add DEM .tif files to that same directory.
popd
exit /b 0

:fail
echo [error] build failed
popd
exit /b 1
