# Builds in an isolated Python 3.12 venv: pythonnet (needed by pywebview) does not support newer Pythons reliably.
if (-not (Test-Path .venv\Scripts\python.exe)) { py -3.12 -m venv .venv }
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m PyInstaller build.spec --noconfirm
Write-Host "Build complete: dist\EmailAgent.exe"
