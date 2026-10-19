"""Assemble the release zip: the packaged app plus the licence files it must carry.

GPLv3 requires the licence text to travel with the binary it covers, so the
release is not "just the exe folder" -- it is the app plus LICENSE,
THIRD-PARTY-NOTICES.md and licenses/GPL-3.0.txt at the archive root, so that
someone who unpacks it has all three without going back to the repo.

The zip root is a single `转码工具\` folder so extracting never scatters 1000+
files into whatever directory the user happened to be in.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "转码工具"
STAGE = ROOT / "_release"
TOP = "转码工具"
# ASCII file name on purpose: the release asset name comes from this, and
# `gh release create` dropped the leading CJK characters, shipping an asset
# literally called "-windows-x64.zip". The folder inside is still 转码工具.
OUT = ROOT / "_release" / "transcode-tool-windows-x64.zip"

EXTRA = ["LICENSE", "THIRD-PARTY-NOTICES.md", "licenses/GPL-3.0.txt", "README.md"]


def main() -> int:
    if not (DIST / "转码工具.exe").exists():
        print("没有找到打包产物，请先运行 python build_exe.py")
        return 1

    # ---- staging tree
    shutil.rmtree(STAGE, ignore_errors=True)
    target = STAGE / TOP
    target.mkdir(parents=True)
    print("复制打包产物（约 227MB）…")
    shutil.copytree(DIST, target, dirs_exist_ok=True, symlinks=False)

    # Runtime state must never ship: it carries the builder's own paths.
    for junk in ("trans.config.json", ".ffcache.json", "启动错误.log"):
        p = target / junk
        if p.exists():
            p.unlink()
            print("  已剔除运行态文件 %s" % junk)

    for rel in EXTRA:
        src = ROOT / rel
        if not src.exists():
            print("  缺少 %s，跳过" % rel)
            continue
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print("  已加入 %s" % rel)

    # ---- verify nothing personal slipped in
    bad: list[str] = []
    for f in target.rglob("*"):
        if not f.is_file() or f.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "D:\\AI\\idea" in text or "C:\\Users\\Justin" in text:
            bad.append(str(f.relative_to(STAGE)))
    if bad:
        print("!! 发现本机路径，停止打包：%s" % bad[:5])
        return 1
    print("  本机路径检查通过")

    # ---- zip
    if OUT.exists():
        OUT.unlink()
    count = 0
    print("压缩中…")
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in sorted(target.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(STAGE).as_posix())
                count += 1

    size = OUT.stat().st_size
    print()
    print("发布包: %s" % OUT.name)
    print("  压缩前 %.1f MB / 压缩后 %.1f MB（%d 个文件）"
          % (sum(f.stat().st_size for f in target.rglob('*') if f.is_file()) / 1048576,
             size / 1048576, count))
    if size > 2 * 1024 ** 3:
        print("!! 超过 GitHub 单文件 2GB 上限")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
