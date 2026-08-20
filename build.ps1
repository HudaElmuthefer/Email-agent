python -m pip install -r requirements.txt
python -m PyInstaller build.spec --noconfirm
Write-Host "Build complete: dist\EmailAgent.exe"
