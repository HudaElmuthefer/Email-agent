# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for EmailAgent.exe. Run via build.ps1.
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [("app/web", "app/web")]
binaries = []
hiddenimports = [
    "app.autostart",
    "app.chat_agent",
    "app.classifier",
    "app.config",
    "app.db",
    "app.duplicate_alert",
    "app.email_sync",
    "app.file_organizer",
    "app.sync_service",
    "app.webview_api",
    "keyring.backends.Windows",
    "win32timezone",
    "clr",
    "pystray._win32",
]
hiddenimports += collect_submodules("webview")

for package in ("webview", "google.genai"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    ["app/main.py"],
    # main.py does "from app import ...", so the project root must be importable.
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="EmailAgent",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)
