@echo off
title AprovaLab Desktop - Criar EXE unico
echo.
echo Gerando EXE unico...
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --windowed ^
  --onefile ^
  --name AprovaLab ^
  --add-data "templates;templates" ^
  --add-data "static;static" ^
  --collect-all openai ^
  --collect-all pypdf ^
  --collect-all webview ^
  desktop.py

echo.
echo ============================================
echo EXE GERADO
echo.
echo dist\AprovaLab.exe
echo ============================================
pause
