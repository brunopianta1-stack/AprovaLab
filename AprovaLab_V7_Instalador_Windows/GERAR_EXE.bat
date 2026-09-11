@echo off
title AprovaLab Desktop - Criar EXE
echo.
echo Gerando aplicativo Windows...
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --windowed ^
  --name AprovaLab ^
  --add-data "templates;templates" ^
  --add-data "static;static" ^
  --collect-all openai ^
  --collect-all pypdf ^
  --collect-all webview ^
  desktop.py

echo.
echo ============================================
echo APLICATIVO GERADO
echo.
echo Procure em:
echo dist\AprovaLab\AprovaLab.exe
echo ============================================
pause
