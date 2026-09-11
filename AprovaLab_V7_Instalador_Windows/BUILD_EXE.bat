@echo off
title AprovaLab V5 - Gerar EXE
python -m PyInstaller --noconfirm --clean --name AprovaLabV5 --add-data "templates;templates" --add-data "static;static" --collect-all openai --collect-all pypdf app.py
echo.
echo Build concluido. Verifique dist\AprovaLabV5
pause
