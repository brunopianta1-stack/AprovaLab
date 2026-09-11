@echo off
setlocal
title AprovaLab - Build completo

call 01_GERAR_APLICATIVO.bat
if errorlevel 1 exit /b 1

call 02_GERAR_INSTALADOR.bat
if errorlevel 1 exit /b 1

echo.
echo Tudo pronto.
echo Instalador:
echo installer\AprovaLab_Setup.exe
pause
