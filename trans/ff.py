"""FFmpeg discovery, capability probing and encoder verification.

Everything here is derived from measuring the actual binary on this machine,
never from assumptions about the version or the platform. A modern ffmpeg and
a 2020 build expose very different encoder options, and a listed encoder is not
the same as a *working* one -- so every hardware encoder is verified with a real
trial encode before it is ever offered to the user.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


def _app_dir() -> Path:
    """Where persistent files belong.

    Frozen, this is the folder holding the exe -- the user's own directory, which
    survives the run. `__file__` would point inside PyInstaller's bundle instead,
    which is a throwaway temp folder for a onefile build.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _bundle_dir() -> Path:
    """Where bundled resources were unpacked (PyInstaller sets _MEIPASS)."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else _app_dir()


APP_DIR = _app_dir()
BUNDLE_DIR = _bundle_dir()
# A copy next to the exe outranks the bundled one, so a user can drop in a newer
# ffmpeg without rebuilding.
USER_BIN_DIR = APP_DIR / "bin"
BIN_DIR = BUNDLE_DIR / "bin"


def _state_dir() -> Path:
    """A writable place for the config and the capability cache.

    Next to the exe when that is writable (portable installs), otherwise under
    LOCALAPPDATA, because an install in Program Files is read-only.
    """
    candidates = [APP_DIR]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "转码工具")
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".write_probe"
            probe.write_text("x", "utf-8")
            probe.unlink()
            return cand
        except OSError:
            continue
    return Path(tempfile.gettempdir())


STATE_DIR = _state_dir()
CONFIG_FILE = STATE_DIR / "trans.config.json"
CACHE_FILE = STATE_DIR / ".ffcache.json"

IS_WINDOWS = os.name == "nt"
# Keep ffmpeg from flashing a console window when frozen/launched via pythonw.
_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
_EXE_SUFFIX = ".exe" if IS_WINDOWS else ""
# Bump when the capability parsers change; see _cache_key.
CACHE_VERSION = 5

# Chromium reads stdin, so this is a real problem for some muxers; not used here
# but kept for clarity of intent when we pass '-y'.
_STDIN = subprocess.DEVNULL


def _popen_kwargs() -> dict:
    kw = {"stdin": _STDIN, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if IS_WINDOWS:
        kw["creationflags"] = _NO_WINDOW
    return kw


def run(exe: str | Path, args, timeout: float = 30.0) -> tuple[int, str, str]:
    """Run a command, returning (returncode, stdout, stderr). Never raises on
    a non-zero exit; raises only on timeout."""
    cmd = [str(exe), *[str(a) for a in args]]
    try:
        p = subprocess.run(cmd, timeout=timeout, **_popen_kwargs())
    except subprocess.TimeoutExpired as exc:
        return 1, "", f"timed out after {timeout:g}s: {exc}"
    dec = lambda b: (b or b"").decode("utf-8", "replace")
    return p.returncode, dec(p.stdout), dec(p.stderr)


# ---------------------------------------------------------------- discovery

def _bin_dirs() -> list[Path]:
    """bin folders to search, most specific first.

    When not frozen both are the same folder; a bundled build has the shipped
    copy inside the bundle and leaves the exe's own bin/ free for a user swap.
    """
    dirs: list[Path] = []
    for d in (USER_BIN_DIR, BIN_DIR):
        if d not in dirs:
            dirs.append(d)
    return dirs


def _candidates(name: str):
    """Ordered search path for an ffmpeg-family binary."""
    exe = name + _EXE_SUFFIX
    for d in _bin_dirs():
        yield d / exe

    found = shutil.which(name)
    if found:
        yield Path(found)

    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    program_data = os.environ.get("ProgramData", "")
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))

    explicit = [
        Path(local) / "Microsoft" / "WinGet" / "Links" / exe,
        Path(program_data) / "chocolatey" / "bin" / exe,
        home / "scoop" / "shims" / exe,
        home / "scoop" / "apps" / "ffmpeg" / "current" / "bin" / exe,
        Path("C:/ffmpeg/bin") / exe,
        Path("C:/ffmpeg") / exe,
        Path(local) / "Programs" / "ffmpeg" / "bin" / exe,
        Path(roaming) / "ffmpeg" / "bin" / exe,
        Path("/usr/bin") / name,
        Path("/usr/local/bin") / name,
        Path("/opt/homebrew/bin") / name,
    ]
    for p in explicit:
        yield p

    # Wide sweep: third-party apps (Tecplot, OBS, Blender, ...) ship their own
    # ffmpeg, and any working one beats no ffmpeg at all.
    roots = [Path("C:/Program Files"), Path("C:/Program Files (x86)"),
             Path(local) / "Programs", Path("C:/ProgramData")]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for sub in root.iterdir():
                if not sub.is_dir():
                    continue
                for depth in (0, 1, 2):
                    pattern = "/".join(["*"] * depth + [exe]) if depth else exe
                    try:
                        for hit in sub.glob(pattern):
                            if hit.is_file():
                                yield hit
                    except OSError:
                        pass
        except (OSError, PermissionError):
            continue


def _verify(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        rc, out, err = run(path, ["-hide_banner", "-version"], timeout=15)
    except Exception:
        return False
    return rc == 0 and "ffmpeg version" in (out + err)


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
    except OSError:
        pass


class FFNotFound(RuntimeError):
    def __init__(self, name: str, tried: int):
        super().__init__(f"未找到 {name}")
        self.name = name
        self.tried = tried


def find_ffmpeg(manual: str | None = None) -> Path:
    if manual:
        p = Path(manual)
        if _verify(p):
            return p
    # A copy the user placed in a bin/ folder is a deliberate choice and outranks
    # both a previously remembered path and whatever happens to be on PATH --
    # otherwise dropping in a newer build has no effect.
    for d in _bin_dirs():
        bundled = d / ("ffmpeg" + _EXE_SUFFIX)
        if _verify(bundled):
            resolved = bundled.resolve()
            cfg = load_config()
            if cfg.get("ffmpeg") != str(resolved):
                cfg["ffmpeg"] = str(resolved)
                save_config(cfg)
            return resolved
    cfg = load_config()
    remembered = cfg.get("ffmpeg")
    if remembered and _verify(Path(remembered)):
        return Path(remembered)
    tried = 0
    for cand in _candidates("ffmpeg"):
        tried += 1
        if _verify(cand):
            resolved = cand.resolve()
            cfg["ffmpeg"] = str(resolved)
            save_config(cfg)
            return resolved
    raise FFNotFound("ffmpeg", tried)


def find_ffprobe(manual: str | None = None) -> Path | None:
    """ffprobe is optional: we can parse `ffmpeg -i` when it is absent."""
    if manual:
        p = Path(manual)
        if p.is_file():
            return p
    # Same preference order as ffmpeg, so the pair always comes from one build.
    for d in _bin_dirs():
        bundled = d / ("ffprobe" + _EXE_SUFFIX)
        if bundled.is_file():
            return bundled.resolve()
    chosen_ffmpeg = None
    cfg = load_config()
    if cfg.get("ffmpeg"):
        chosen_ffmpeg = Path(cfg["ffmpeg"])
        sibling = chosen_ffmpeg.with_name("ffprobe" + _EXE_SUFFIX)
        if sibling.is_file():
            return sibling
    remembered = cfg.get("ffprobe")
    if remembered and Path(remembered).is_file():
        return Path(remembered)
    found = shutil.which("ffprobe")
    if found:
        return Path(found).resolve()
    for cand in _candidates("ffprobe"):
        if cand.is_file():
            return cand.resolve()
    return None


# --------------------------------------------------------------- capability

# Codec names as ffmpeg reports them in `-i` output -> our internal family.
_CODEC_FAMILY = {
    "avc1": "h264", "h264": "h264", "x264": "h264",
    "hvc1": "hevc", "hev1": "hevc", "hevc": "hevc", "h265": "hevc", "x265": "hevc",
    "mp4v": "mpeg4", "mpeg4": "mpeg4", "xvid": "mpeg4", "divx": "mpeg4",
    "vp8": "vp8", "vp09": "vp9", "vp9": "vp9",
    "av01": "av1", "av1": "av1",
    "mjpeg": "mjpeg", "jpeg": "mjpeg", "jpegls": "mjpeg",
    "png": "png", "apng": "png", "webp": "webp", "bmp": "bmp",
    "tiff": "tiff", "gif": "gif",
    "prores": "prores", "mpeg2video": "mpeg2video", "mpeg1video": "mpeg1video",
    "theora": "theora", "wmv1": "wmv1", "wmv2": "wmv2", "wmv3": "wmv3", "vc1": "vc1",
    "flv1": "flv1", "msmpeg4v3": "mpeg4",
    "aac": "aac", "mp3": "mp3", "mp2": "mp2", "ac3": "ac3", "eac3": "eac3",
    "flac": "flac", "opus": "opus", "vorbis": "vorbis", "alac": "alac",
    "pcm_s16le": "pcm_s16le", "pcm_s24le": "pcm_s24le", "pcm_u8": "pcm_u8",
    "pcm_f32le": "pcm_f32le", "wmav2": "wmav2", "amr_nb": "amr_nb", "aac_latm": "aac",
    "dts": "dts", "truehd": "truehd", "pcm_s16be": "pcm_s16be",
}


def codec_family(name: str) -> str:
    if not name:
        return ""
    return _CODEC_FAMILY.get(name.lower(), name.lower())


class Capabilities:
    """Measured facts about the resolved ffmpeg binary."""

    def __init__(self, ffmpeg: Path, ffprobe: Path | None):
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.version = ""
        self.version_tuple = (0, 0)
        self.encoders: set[str] = set()
        self.decoders: set[str] = set()
        self.filters: set[str] = set()
        self.muxers: set[str] = set()
        self.demuxers: set[str] = set()
        self.working_encoders: dict[str, bool] = {}
        self.animated_webp_input = True
        # Arguments that make ffmpeg pass source frame timestamps straight
        # through instead of resampling to an output frame rate. Without this,
        # a file whose header claims a millisecond timebase (ffprobe reports
        # r_frame_rate=1000/1 for VFR screen recordings) gets its frames
        # duplicated up to that rate -- 8 seconds of video became 8000 frames.
        self.passthrough_args: list[str] = []
        self.notes: list[str] = []
        self._lock = threading.Lock()

    # -- name-based queries -------------------------------------------------
    def has_encoder(self, name: str) -> bool:
        return name in self.encoders

    def has_filter(self, name: str) -> bool:
        return name in self.filters

    def has_muxer(self, name: str) -> bool:
        return name in self.muxers

    def encoding_works(self, name: str) -> bool:
        """True only if a real trial encode succeeded.

        Keyed by the ffmpeg encoder name (`h264_amf`), not by the app's profile
        key (`amf_h264`): callers hold VideoEncoder objects and pass `.encoder`,
        and keying by anything else silently reported every hardware encoder as
        unusable.
        """
        return self.working_encoders.get(name, False)

    # -- probing ------------------------------------------------------------
    def probe(self, cache_ok: bool = True) -> "Capabilities":
        key = None
        if cache_ok:
            key = _cache_key(self.ffmpeg)
            cached = _read_cache(key)
            if cached:
                self._adopt(cached)
                return self

        rc, out, err = run(self.ffmpeg, ["-hide_banner", "-version"], timeout=20)
        text = out + err
        m = re.search(r"ffmpeg version (\S+)", text)
        if m:
            self.version = m.group(1)
            nums = re.findall(r"\d+", self.version)
            if nums:
                self.version_tuple = (int(nums[0]), int(nums[1]) if len(nums) > 1 else 0)
        # Configuration line tells us what the build was compiled with.
        cfg_m = re.search(r"^configuration: (.*)$", text, re.M)
        if cfg_m:
            cfg_line = cfg_m.group(1)
            for feats in ("libx264", "libx265", "libvpx", "libwebp", "libmp3lame",
                          "libopus", "libvorbis", "libaom", "librsvg", "libsvtav1",
                          "fontconfig", "libfreetype"):
                if feats not in cfg_line and feats in ("libx264", "libmp3lame"):
                    self.notes.append(f"该构建未编译 {feats}")

        self.encoders = self._list("-encoders")
        self.decoders = self._list("-decoders")
        self.filters = self._list("-filters")
        self.muxers = self._list("-muxers")
        self.demuxers = self._list("-demuxers")

        # Animated WebP input needs the `webp_anim` demuxer; ffmpeg 4.3 shipped
        # only `webp_pipe`, which reads a single frame. Testing for a demuxer
        # literally named `webp` was wrong -- no build has ever had one, so the
        # condition held on every build that had webp_pipe and the app told the
        # user animation could not be read even on builds where it could.
        self.animated_webp_input = "webp_anim" in self.demuxers
        if not self.animated_webp_input:
            self.notes.append("该构建只能读取静态 WebP，动画 WebP 仅能作为输出格式")

        self._verify_passthrough()

        if key:
            _write_cache(key, self._snapshot())
        return self

    def _list(self, flag: str) -> set[str]:
        """Parse an `-encoders` / `-filters` / `-muxers` style listing.

        The flag column differs per listing (`V.....` for encoders, `T..` for
        filters, often absent for muxers), so each shape is matched explicitly
        rather than by guessing at token positions.
        """
        kind = flag.lstrip("-")
        headers = {"encoders": "Encoders:", "decoders": "Decoders:",
                   "filters": "Filters:", "muxers": "Muxers:",
                   "demuxers": "Demuxers:"}
        header = headers.get(kind, "")
        rc, out, err = run(self.ffmpeg, ["-hide_banner", flag], timeout=25)
        lines = (out + err).splitlines()
        start = 0
        for i, line in enumerate(lines):
            if line.strip() == header:
                start = i + 1
                break

        names: set[str] = set()
        for line in lines[start:]:
            if not line.strip():
                continue
            if kind in ("encoders", "decoders"):
                m = re.match(r"^\s*[A-Z.]{6}\s+(\S+)", line)
            elif kind == "filters":
                # Anchor on the distinctive "in->out" column.
                m = re.match(r"^\s*(?:[A-Z.]{1,4}\s+)?(\S+)\s+\S+->\S+", line)
            else:
                m = re.match(r"^\s*(?:[A-Z]\s+)?(\S+)\s", line)
            if m:
                names.add(m.group(1))
        return names

    def _snapshot(self) -> dict:
        return {
            "version": self.version,
            "encoders": sorted(self.encoders),
            "decoders": sorted(self.decoders),
            "filters": sorted(self.filters),
            "muxers": sorted(self.muxers),
            "demuxers": sorted(self.demuxers),
            "working_encoders": self.working_encoders,
            "animated_webp_input": self.animated_webp_input,
            "passthrough_args": self.passthrough_args,
            "notes": self.notes,
        }

    def _adopt(self, data: dict) -> None:
        self.version = data.get("version", "")
        self.version_tuple = tuple(int(x) for x in re.findall(r"\d+", self.version)[:2]) or (0, 0)
        self.encoders = set(data.get("encoders", []))
        self.decoders = set(data.get("decoders", []))
        self.filters = set(data.get("filters", []))
        self.muxers = set(data.get("muxers", []))
        self.demuxers = set(data.get("demuxers", []))
        self.working_encoders = dict(data.get("working_encoders", {}))
        self.animated_webp_input = bool(data.get("animated_webp_input", True))
        self.passthrough_args = list(data.get("passthrough_args", []))
        self.notes = list(data.get("notes", []))

    # -- encoder verification ----------------------------------------------
    def _verify_passthrough(self) -> None:
        """Pick whichever spelling of "don't change the frame timing" works.

        ffmpeg 5.0 renamed -vsync to -fps_mode; a 4.x build rejects -fps_mode
        outright. Probing beats guessing at version numbers, and the result is
        cached with the rest of the capability snapshot.
        """
        for args in (["-fps_mode", "passthrough"], ["-vsync", "0"]):
            rc, _, _ = run(self.ffmpeg, [
                "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "testsrc2=size=64x64:rate=10:duration=0.3",
                "-c:v", "mpeg4", *args, "-frames:v", "3", "-f", "null", os.devnull],
                timeout=60)
            if rc == 0:
                self.passthrough_args = args
                return
        self.notes.append("未能确定帧率透传参数，可变帧率素材可能被重复插帧")

    def verify_encoders(self, profiles: dict) -> None:
        """Trial-encode with each candidate hardware encoder.

        Presence in `-encoders` proves nothing: an old build links NVENC
        headers that a current driver refuses ('unsupported param'), and the
        failure only surfaces at encode time.
        """
        with self._lock:
            todo = [n for n in profiles
                    if profiles[n].get("hardware")
                    and self.has_encoder(profiles[n]["codec"])
                    and profiles[n]["codec"] not in self.working_encoders]
        for name in todo:
            self.working_encoders[profiles[name]["codec"]] = \
                self._trial_encode(profiles[name])
        _write_cache(_cache_key(self.ffmpeg), self._snapshot())

    def _trial_encode(self, spec: dict) -> bool:
        args = ["-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "testsrc2=size=640x360:rate=15:duration=0.4",
                "-c:v", spec["codec"], *spec.get("smoke", []), "-frames:v", "4",
                "-f", "null", os.devnull]
        try:
            rc, _, _ = run(self.ffmpeg, args, timeout=60)
        except Exception:
            return False
        return rc == 0


def _cache_key(ffmpeg: Path) -> str:
    """Bump CACHE_VERSION whenever the parsers change.

    The key includes the binary's fingerprint, so a rebuilt or replaced ffmpeg
    is picked up automatically -- but a change in how we *parse* its output
    leaves the fingerprint identical, and a stale snapshot would then quietly
    under-report what the build can do.
    """
    try:
        st = ffmpeg.stat()
        return f"v{CACHE_VERSION}|{ffmpeg}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return f"v{CACHE_VERSION}|{ffmpeg}"


def _read_cache(key: str) -> dict | None:
    try:
        data = json.loads(CACHE_FILE.read_text("utf-8"))
    except Exception:
        return None
    entry = data.get(key)
    return entry if isinstance(entry, dict) else None


def _write_cache(key: str, value: dict) -> None:
    try:
        data = json.loads(CACHE_FILE.read_text("utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    # Keep the file small: only ever remember the ffmpeg we are using.
    data = {key: value}
    try:
        CACHE_FILE.write_text(json.dumps(data), "utf-8")
    except OSError:
        pass


_SINGLETON: Capabilities | None = None
_SINGLETON_LOCK = threading.Lock()


def detect(force: bool = False) -> Capabilities:
    """Resolve and probe ffmpeg, memoised for the process lifetime."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is not None and not force:
            return _SINGLETON
        ffmpeg = find_ffmpeg()
        ffprobe = find_ffprobe()
        caps = Capabilities(ffmpeg, ffprobe).probe(cache_ok=not force)
        _SINGLETON = caps
        return caps
