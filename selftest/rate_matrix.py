"""Every 格式 × 采样率 × 码率 combination the UI can produce.

The dropdowns are independent, so any pair a user can pick must work. libvorbis
refuses 192 kbps at 16 kHz mono outright ("encoder setup failed"), and libmp3lame
has similar per-rate limits -- so the matrix is swept exhaustively rather than
guessed at.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import FFMPEG  # noqa: E402

# The repo root is wherever this file ended up, so a clone runs anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from trans import media, plan  # noqa: E402
from trans.presets import (AUDIO_CONTAINERS, BITRATES_KBPS, SAMPLE_RATES,  # noqa: E402
                           AudioOptions)

WORK = ROOT / "_probe_rates"
WORK.mkdir(exist_ok=True)
SRC = WORK / "src.wav"
if not SRC.exists():
    subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
                    "-c:a", "pcm_s16le", str(SRC)], check=True)

CONTAINERS = [c for c in AUDIO_CONTAINERS if c != "wav"]
info = media.probe(SRC, use_cache=False)

failures: list[tuple[str, int, int, int, str]] = []
total = 0
for container in CONTAINERS:
    for channels in (1, 2):
        for rate in SAMPLE_RATES:
            # keep the sweep proportionate: 9 bitrates on a representative set
            for bitrate in BITRATES_KBPS:
                total += 1
                opts = AudioOptions(container=container, bitrate=bitrate,
                                    sample_rate=rate, channels=channels,
                                    copy_stream=False)
                job = plan.plan_audio(info, WORK, opts)
                if not job.steps:
                    continue
                args = job.steps[0].args
                out = WORK / ("t_%s_%d_%d_%d.%s" % (container, rate, channels,
                                                    bitrate, container))
                p = subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error",
                                    "-y", *args[:-1], str(out)],
                                   capture_output=True)
                ok = p.returncode == 0 and out.exists() and out.stat().st_size > 0
                if not ok:
                    err = p.stderr.decode("utf-8", "replace").strip()
                    msg = next((l for l in err.splitlines() if l.strip()), "")
                    failures.append((container, rate, channels, bitrate,
                                     msg[:110]))
                if out.exists():
                    out.unlink()
                job.cleanup()

print("组合总数: %d" % total)
print("失败组合: %d" % len(failures))
print()
by_codec: dict[str, list] = {}
for f in failures:
    by_codec.setdefault(f[0], []).append(f)
for container, items in by_codec.items():
    print("=== %s 失败 %d 项" % (container, len(items)))
    for _c, rate, ch, br, msg in items[:14]:
        print("    %6dHz %dch %4dkbps  %s" % (rate, ch, br, msg))
    if len(items) > 14:
        print("    ...（共 %d 项）" % len(items))
    print()

print("=== 失败组合里的采样率/码率分布 ===")
for container, items in by_codec.items():
    pairs = sorted({(r, br) for _c, r, _ch, br, _m in items})
    print("  %-6s %s" % (container, pairs[:20]))
