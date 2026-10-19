"""Build a standalone Windows bundle with PyInstaller.

    python build_exe.py            -> dist/转码工具/   (folder, starts instantly)
    python build_exe.py --onefile  -> dist/转码工具.exe (single file, unpacks on
                                      every launch -- with a 200MB ffmpeg inside
                                      that means a long wait each time)

The folder build is the default because ffmpeg alone is ~200MB; a onefile build
would unpack all of it to a temp folder on every single launch.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "转码工具"
ENTRY = ROOT / f"{NAME}.pyw"
ICON = ROOT / "assets" / "app.ico"
BIN = ROOT / "bin"

# Nothing here is imported by the app; excluding them keeps the bundle honest.
EXCLUDES = ["numpy", "PIL", "matplotlib", "scipy", "pandas", "PyQt5", "PyQt6",
            "PySide2", "PySide6", "IPython", "notebook", "pytest", "cv2",
            "sqlite3", "pydoc_data", "lib2to3", "distutils"]


def make_icon() -> bool:
    """A simple, legible mark: rounded blue tile with a white play triangle."""
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:
        print("  (跳过图标：%s)" % exc)
        return False
    ICON.parent.mkdir(parents=True, exist_ok=True)
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8, 8, size - 8, size - 8], radius=52, fill=(37, 99, 235, 255))
    d.rounded_rectangle([8, 8, size - 8, size - 8], radius=52,
                        outline=(23, 66, 158, 255), width=6)
    # play triangle, nudged right so it looks centred
    d.polygon([(104, 66), (104, 190), (196, 128)], fill=(255, 255, 255, 255))
    img.save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                          (128, 128), (256, 256)])
    print("  图标: %s" % ICON)
    return True


def build(onefile: bool) -> int:
    if not ENTRY.exists():
        print("找不到入口文件:", ENTRY)
        return 1
    for needed in ("ffmpeg.exe", "ffprobe.exe"):
        if not (BIN / needed).exists():
            print("bin/%s 不存在，先把 ffmpeg 放进 bin 目录" % needed)
            return 1

    print("清理旧的构建产物…")
    for d in ("build", "dist"):
        shutil.rmtree(ROOT / d, ignore_errors=True)
    shutil.rmtree(ROOT / "__pycache__", ignore_errors=True)
    shutil.rmtree(ROOT / "trans" / "__pycache__", ignore_errors=True)
    shutil.rmtree(ROOT / "trans" / "ui" / "__pycache__", ignore_errors=True)

    print("准备图标…")
    icon_ok = make_icon()

    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--windowed", "--name", NAME,
            "--add-data", "%s;bin" % (BIN / "ffmpeg.exe"),
            "--add-data", "%s;bin" % (BIN / "ffprobe.exe"),
            # tkinterdnd2 ships a native tkdnd library that must be collected
            "--collect-all", "tkinterdnd2"]
    if icon_ok:
        args += ["--icon", str(ICON)]
    for mod in EXCLUDES:
        args += ["--exclude-module", mod]
    if onefile:
        args += ["--onefile"]
    args.append(str(ENTRY))

    print()
    print("执行: %s" % " ".join(args[2:6]), "…")
    print()
    t0 = time.time()
    p = subprocess.run(args, cwd=ROOT)
    print()
    print("PyInstaller 退出码 %d，耗时 %.1f 秒" % (p.returncode, time.time() - t0))
    if p.returncode != 0:
        return p.returncode

    dist = ROOT / "dist"
    if onefile:
        target = dist / f"{NAME}.exe"
        if target.exists():
            print("产物: %s  %.1f MB" % (target, target.stat().st_size / 1024 / 1024))
    else:
        folder = dist / NAME
        exe = folder / f"{NAME}.exe"
        if not exe.exists():
            print("没有找到预期的 exe:", exe)
            return 1
        total = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
        print("产物目录: %s" % folder)
        print("  入口: %s" % exe.name)
        print("  总计: %.1f MB（其中 ffmpeg/ffprobe 约 200MB）" % (total / 1024 / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(build("--onefile" in sys.argv))
