@echo off
setlocal
title AprovaLab - Criar instalador

echo ============================================
echo  APROVALAB - BUILD DO SETUP.EXE
echo ============================================
echo.

set "ISCC="

if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if "%ISCC%"=="" (
  echo Inno Setup 6 nao foi encontrado.
  echo Instale o Inno Setup 6 e execute este arquivo novamente.
  echo.
  echo Site oficial:
  echo https://jrsoftware.org/isinfo.php
  pause
  exit /b 1
)

if not exist "dist\AprovaLab\AprovaLab.exe" (
  echo O aplicativo ainda nao foi compilado.
  echo Execute primeiro: 01_GERAR_APLICATIVO.bat
  pause
  exit /b 1
)

"%ISCC%" "AprovaLab_Setup.iss"
if errorlevel 1 (
  echo.
  echo Falha ao gerar o instalador.
  pause
  exit /b 1
)

echo.
echo ============================================
echo INSTALADOR GERADO:
echo installer\AprovaLab_Setup.exe
echo ============================================
pause
