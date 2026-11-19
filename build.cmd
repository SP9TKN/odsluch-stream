@echo off
rem Odsluch stream: one-file Windows exe without a console window (PyInstaller).
rem Run from this folder: build.cmd   ->   dist\Odsluch_stream.exe
rem domyslne.py (your private default stream) is left out on purpose (--exclude-module domyslne).
cd /d "%~dp0"
py -m PyInstaller --noconfirm --onefile --noconsole --name Odsluch_stream --icon czajnik.ico ^
  --add-data "czajnik.ico;." ^
  --add-binary "rnnoise/rnnoise.dll;rnnoise" ^
  --add-data "rnnoise/LICENSE-pyrnnoise.txt;rnnoise" ^
  --add-data "rnnoise/COPYING-rnnoise.txt;rnnoise" ^
  --add-data "licencje_av;licencje_av" ^
  --exclude-module PIL --exclude-module domyslne ^
  odsluch.py
