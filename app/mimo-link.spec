# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for MiMo Link desktop release (Windows).

Build:
    pyinstaller mimo-link.spec
Output:
    dist/MiMoLink/MiMoLink.exe   (onedir — smaller startup, easy to zip)
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 — SPECPATH is injected by PyInstaller
APP = ROOT / "app"

block_cipher = None

a = Analysis(
    [str(APP / "desktop.py")],
    pathex=[str(ROOT), str(APP)],
    binaries=[],
    datas=[
        (str(APP / "index.html"), "app"),
        (str(APP / "assets"), "app/assets"),
    ],
    hiddenimports=[
        "server",
        "mimo_link_core",
        "key_store",
        "pricing",
        "responses_bridge",
        "schemas",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MiMoLink",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed release; set True when debugging
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(APP / "assets" / "app.ico") if (APP / "assets" / "app.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MiMoLink",
)
