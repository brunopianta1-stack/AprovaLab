@echo off
setlocal
title AprovaLab - Compilar aplicativo

echo ============================================
echo  APROVALAB - BUILD DO APLICATIVO
echo ============================================
echo.

python -m pip install -r requirements.txt
if errorlevel 1 goto :erro

python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --windowed ^
  --name AprovaLab ^
  --icon "AprovaLab.ico" ^
  --add-data "templates;templates" ^
  --add-data "static;static" ^
  --collect-all openai ^
  --collect-all pypdf ^
  --collect-all webview ^
  desktop.py

if errorlevel 1 goto :erro

echo.
echo Aplicativo compilado com sucesso:
echo dist\AprovaLab\AprovaLab.exe
echo.
goto :fim

:erro
echo.
echo ERRO AO COMPILAR.
pause
exit /b 1

:fim
pause
