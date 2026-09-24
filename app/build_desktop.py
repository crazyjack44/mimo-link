#!/usr/bin/env python3
"""Build the MiMo Link Windows desktop release with PyInstaller.

Usage:
    python app/build_desktop.py

Creates:
    app/assets/app.ico  (from ml-avatar-2.png)
    dist/MiMoLink/      (zip this folder for GitHub Release)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
ASSETS = APP / "assets"
PNG = ASSETS / "ml-avatar-2.png"
ICO = ASSETS / "app.ico"
SPEC = APP / "mimo-link.spec"


def ensure_icon() -> None:
    if ICO.exists():
        return
    if not PNG.exists():
        print(f"skip icon: {PNG} not found")
        return
    from PIL import Image

    img = Image.open(PNG).convert("RGBA")
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(ICO, format="ICO", sizes=sizes)
    print(f"wrote {ICO}")


def main() -> int:
    ensure_icon()
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        str(SPEC),
    ]
    print("running:", " ".join(cmd))
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        return rc
    out = ROOT / "dist" / "MiMoLink"
    print("\nBuild OK:", out)
    print("Zip that folder and attach it to a GitHub Release.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
