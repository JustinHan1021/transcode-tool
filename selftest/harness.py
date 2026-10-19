"""Shared plumbing for the functional sweeps.

The point of these checks is to look at what actually landed on disk -- codec,
dimensions, duration, frame count -- rather than at the command we meant to run.
A plan can be perfectly formed and still produce the wrong file.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# The repo root is wherever this file ended up, so a clone runs anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trans import ff, media, plan  # noqa: E402
from trans.engine import Engine, JobResult  # noqa: E402

CAPS = ff.detect()
FFMPEG = CAPS.ffmpeg
FFPROBE = CAPS.ffprobe
CORPUS = ROOT / "_corpus"
WORK = ROOT / "_probe_work"

_results: list[tuple[str, bool, str]] = []
_section = ""


def begin(name: str) -> None:
    global _section
    _section = name
    print()
    print("=" * 78)
    print(name)
    print("=" * 78)


def check(label: str, ok: bool, detail: str = "") -> bool:
    _results.append((f"{_section} / {label}", ok, detail))
    mark = "OK  " if ok else "FAIL"
    line = "  [%s] %s" % (mark, label)
    if detail:
        line += "  -- " + detail
    print(line)
    return ok


def run_job(job: plan.Job, concurrency: int = 1) -> JobResult:
    got: list[JobResult] = []
    Engine(FFMPEG, log_lines=40).run([job], concurrency=concurrency,
                                     on_complete=lambda rs: got.extend(rs))
    return got[0] if got else JobResult(job=job, error="引擎没有返回结果")


def run_plan_direct(job: plan.Job) -> tuple[bool, str]:
    """Run a job's steps with plain subprocess, keeping ffmpeg's own log.

    The engine swallows stderr, so it cannot answer "did this really copy the
    video stream?" -- ffmpeg's `Stream mapping` line can, and that is the only
    account of the decision that is not our own claim about it.
    """
    log: list[str] = []
    for step in job.steps:
        if step.prepare:
            try:
                step.prepare()
            except Exception as exc:
                return False, "prepare failed: %s" % exc
        p = subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "info",
                            "-progress", "pipe:1", "-y", *[str(a) for a in step.args]],
                           capture_output=True)
        log.append(p.stderr.decode("utf-8", "replace"))
    hard = [l for l in "\n".join(log).splitlines() if " -> " in l]
    mapping = [l.strip() for l in hard if l.strip().startswith("Stream #")]
    return all(l for l in [True]), "\n".join(mapping)


def video_copied(mapping: str) -> bool | None:
    """From ffmpeg's Stream mapping: was the first video stream copied?"""
    for line in mapping.splitlines():
        if line.startswith("Stream #0:0 ->") or " -> #0:0" in line:
            return "(copy)" in line
    return None


# ------------------------------------------------------------------ inspection

def probe_raw(path: Path) -> dict:
    """ffprobe JSON for an output file, or {} when unreadable."""
    if FFPROBE is None:
        return {}
    rc, out, err = ff.run(FFPROBE, ["-hide_banner", "-v", "quiet",
                                    "-print_format", "json", "-show_format",
                                    "-show_streams", str(path)], timeout=120)
    if rc != 0 or not out.strip():
        return {}
    try:
        return json.loads(out)
    except ValueError:
        return {}


class Out:
    """The facts about one produced file."""

    def __init__(self, path: Path):
        self.path = path
        self.exists = path.exists()
        self.size = path.stat().st_size if self.exists else 0
        data = probe_raw(path) if self.exists else {}
        self.data = data
        fmt = data.get("format") or {}
        self.container = fmt.get("format_name", "")
        streams = data.get("streams") or []
        self.video = next((s for s in streams if s.get("codec_type") == "video"), None)
        self.audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        try:
            self.duration = float(fmt.get("duration") or 0.0)
        except (TypeError, ValueError):
            self.duration = 0.0
        self.nb_streams = int(fmt.get("nb_streams") or 0)

    @property
    def vcodec(self) -> str:
        return (self.video or {}).get("codec_name", "")

    @property
    def acodec(self) -> str:
        return (self.audio or {}).get("codec_name", "")

    @property
    def width(self) -> int:
        return int((self.video or {}).get("width") or 0)

    @property
    def height(self) -> int:
        return int((self.video or {}).get("height") or 0)

    @property
    def dims(self) -> tuple[int, int]:
        return self.width, self.height

    @property
    def sample_rate(self) -> int:
        try:
            return int((self.audio or {}).get("sample_rate") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def channels(self) -> int:
        return int((self.audio or {}).get("channels") or 0)

    def decodes(self) -> tuple[bool, str]:
        """Decode the whole file to /dev/null; a truncated or lying file fails."""
        rc, out, err = ff.run(FFMPEG, ["-hide_banner", "-nostats", "-v", "error",
                                       "-i", str(self.path), "-f", "null",
                                       os.devnull], timeout=600)
        return rc == 0, (err or "").strip()[:200]

    def frames(self) -> int:
        rc, out, err = ff.run(FFMPEG, ["-hide_banner", "-nostats", "-loglevel",
                                       "info", "-i", str(self.path), "-f", "null",
                                       os.devnull], timeout=600)
        import re
        hits = re.findall(r"frame=\s*(\d+)", err + out)
        return int(hits[-1]) if hits else -1

    def describe(self) -> str:
        bits = [self.vcodec or "-"]
        if self.video:
            bits.append("%dx%d" % (self.width, self.height))
        if self.audio:
            bits.append(self.acodec or "-")
        bits.append("%.2fs" % self.duration)
        bits.append("%.0fKB" % (self.size / 1024))
        return " ".join(bits)


# ------------------------------------------------------------------ fixtures

_cache: dict[str, Path] = {}


def short_clip(name: str = "s1080.mp4", seconds: float = 3.0,
               size: str = "1920x1080", fps: int = 30) -> Path:
    """A short clip for encode tests: fast to run, still realistic."""
    key = "%s|%s|%s|%s" % (name, seconds, size, fps)
    if key in _cache:
        return _cache[key]
    WORK.mkdir(parents=True, exist_ok=True)
    out = WORK / name
    if not out.exists():
        subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=s=%s:r=%d:d=%g"
                        % (size, fps, seconds),
                        "-f", "lavfi", "-i",
                        "sine=frequency=520:sample_rate=48000:duration=%g" % seconds,
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                        "-shortest", str(out)], check=True)
    _cache[key] = out
    return out


def temp_dir(name: str) -> Path:
    d = WORK / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def stream_hash(path: Path, kind: str = "v") -> str:
    """md5 over the raw packets of one stream.

    Identical output means the stream was genuinely copied rather than
    re-encoded and merely reported as copied -- which a duration or codec-name
    check cannot tell apart.
    """
    rc, out, err = ff.run(FFMPEG, ["-hide_banner", "-v", "error", "-i", str(path),
                                   "-map", "0:%s:0" % kind, "-c", "copy",
                                   "-f", "hash", "-hash", "md5", "-"], timeout=300)
    for line in (out + err).splitlines():
        if line.startswith("MD5="):
            return line[4:].strip()
    return ""


def info_of(path: Path) -> media.MediaInfo:
    return media.probe(path, use_cache=False)


def cleanup() -> None:
    shutil.rmtree(WORK, ignore_errors=True)


# ------------------------------------------------------------------ summary

def summary(title: str) -> int:
    failed = [(n, d) for n, ok, d in _results if not ok]
    print()
    print("=" * 78)
    print("%s: 共 %d 项，通过 %d，失败 %d"
          % (title, len(_results), len(_results) - len(failed), len(failed)))
    if failed:
        print("-" * 78)
        for n, d in failed:
            print("  失败: %s%s" % (n, ("  (%s)" % d) if d else ""))
    print("=" * 78)
    return len(failed)
