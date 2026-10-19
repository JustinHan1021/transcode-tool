"""Concurrent job execution.

Runs each ffmpeg with `-progress pipe:1` and reports parsed progress, so the
window stays responsive and a batch of files finishes in the time one file
would take. Completion, cancellation and failure are all reported through a
single callback; the UI is responsible for marshalling those onto its own
thread.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .plan import Job, Step

NO_WINDOW = 0x08000000


@dataclass
class JobResult:
    job: Job
    ok: bool = False
    cancelled: bool = False
    error: str = ""
    elapsed: float = 0.0
    output_size: int = 0


EventCallback = Callable[[dict], None]


class Engine:
    """A bounded worker pool over Jobs."""

    def __init__(self, ffmpeg: Path, on_event: EventCallback | None = None,
                 log_lines: int = 40):
        self.ffmpeg = Path(ffmpeg)
        self.on_event = on_event
        self.log_lines = log_lines
        self._procs: dict[int, subprocess.Popen] = {}
        self._cancel: set[int] = set()
        self._cancel_all = False
        self._lock = threading.Lock()
        self._running = False

    # ---------------------------------------------------------------- public
    @property
    def is_running(self) -> bool:
        return self._running

    def run(self, jobs: list[Job], concurrency: int = 2,
            on_complete: Callable[[list[JobResult]], None] | None = None) -> None:
        """Blocking: run every job, then fire on_complete. Call from a thread."""
        self._running = True
        self._cancel.clear()
        results: list[JobResult] = []
        results_lock = threading.Lock()
        index = 0
        index_lock = threading.Lock()
        total = len(jobs)

        def worker() -> None:
            nonlocal index
            while True:
                with index_lock:
                    if index >= total:
                        return
                    i = index
                    index += 1
                job = jobs[i]
                res = self._run_job(job, i)
                with results_lock:
                    results.append(res)

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(max(1, min(concurrency, total)))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self._running = False
        self._emit({"type": "drained", "count": total})
        if on_complete:
            ordered = sorted(results, key=lambda r: jobs.index(r.job))
            on_complete(ordered)

    def cancel(self, job_id: int) -> None:
        """Mark a job cancelled and kill its ffmpeg if it is already running."""
        with self._lock:
            self._cancel.add(job_id)
            proc = self._procs.get(job_id)
        if proc is not None:
            _terminate(proc)

    def cancel_all(self) -> None:
        with self._lock:
            self._cancel_all = True
            procs = list(self._procs.values())
        for p in procs:
            _terminate(p)

    # --------------------------------------------------------------- internal
    def _emit(self, payload: dict) -> None:
        if self.on_event:
            try:
                self.on_event(payload)
            except Exception:
                pass

    def _was_cancelled(self, job_id: int) -> bool:
        with self._lock:
            return self._cancel_all or job_id in self._cancel

    def _run_job(self, job: Job, job_id: int) -> JobResult:
        started = time.time()
        res = JobResult(job=job)
        self._emit({"type": "started", "id": job_id, "job": job})

        if not job.steps:
            res.ok = False
            res.error = "没有可执行的动作"
            self._emit({"type": "finished", "id": job_id, "ok": False,
                        "error": res.error, "elapsed": 0.0, "out": job.out})
            return res

        weights = [max(0.001, 1.0) for _ in job.steps]
        total_weight = sum(weights)
        done_weight = 0.0

        try:
            for idx, step in enumerate(job.steps):
                if self._was_cancelled(job_id):
                    res.cancelled = True
                    break
                self._emit({"type": "step", "id": job_id, "index": idx + 1,
                            "count": len(job.steps), "label": step.label})

                def on_step_progress(frac: float, speed: float | None,
                                     _idx=idx, _done=done_weight) -> None:
                    overall = (_done + weights[_idx] * frac) / total_weight
                    self._emit({"type": "progress", "id": job_id,
                                "fraction": max(0.0, min(1.0, overall)),
                                "speed": speed, "step": job.steps[_idx].label})

                rc, err, cancelled = self._run_step(job_id, step, on_step_progress)
                if cancelled:
                    res.cancelled = True
                    break
                if rc != 0:
                    res.ok = False
                    res.error = err or f"ffmpeg 退出码 {rc}"
                    break
                done_weight += weights[idx]
                self._emit({"type": "progress", "id": job_id,
                            "fraction": min(1.0, done_weight / total_weight),
                            "speed": None, "step": step.label})
            else:
                res.ok = True
        except Exception as exc:                      # pragma: no cover - defensive
            res.ok = False
            res.error = f"{type(exc).__name__}: {exc}"
        finally:
            if res.cancelled:
                _discard(job.out)
            elif not res.ok:
                # Leaving a half-written file behind would look like success.
                _discard(job.out)
            job.cleanup()

        res.elapsed = time.time() - started
        try:
            if res.ok and job.out.exists():
                res.output_size = job.out.stat().st_size
        except OSError:
            pass

        self._emit({"type": "finished", "id": job_id, "ok": res.ok,
                    "cancelled": res.cancelled, "error": res.error,
                    "elapsed": res.elapsed, "out": job.out,
                    "size": res.output_size})
        return res

    def _run_step(self, job_id: int, step: Step,
                  on_progress) -> tuple[int, str, bool]:
        if step.prepare is not None:
            try:
                step.prepare()
            except Exception as exc:
                return 1, f"准备工作失败: {exc}", False

        cmd = [str(self.ffmpeg), "-hide_banner", "-nostats", "-loglevel", "warning",
               "-progress", "pipe:1", "-y", *step.args]
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
                  "stderr": subprocess.PIPE}
        if os.name == "nt":
            kwargs["creationflags"] = NO_WINDOW

        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except OSError as exc:
            return 1, f"无法启动 ffmpeg: {exc}", False

        with self._lock:
            self._procs[job_id] = proc

        log: deque[str] = deque(maxlen=self.log_lines)

        def drain() -> None:
            try:
                for raw in proc.stderr:
                    line = raw.decode("utf-8", "replace").rstrip()
                    if line.strip():
                        log.append(line.strip())
            except Exception:
                pass

        err_thread = threading.Thread(target=drain, daemon=True)
        err_thread.start()

        cancelled = False
        current_speed: float | None = None
        processed = 0.0
        last_tick = 0.0
        try:
            for raw in proc.stdout:
                if self._was_cancelled(job_id):
                    cancelled = True
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                key, _, value = line.partition("=")
                if key == "out_time_us":
                    try:
                        processed = int(value) / 1_000_000
                        frac = (min(0.999, max(0.0, processed / step.expected))
                                if step.expected else None)
                        now = time.time()
                        # Throttle: -progress emits several lines per update.
                        if now - last_tick >= 0.4:
                            last_tick = now
                            on_progress(frac if frac is not None else 0.0, current_speed)
                            self._emit({"type": "tick", "id": job_id,
                                        "step": step.label,
                                        "processed": processed,
                                        "expected": step.expected,
                                        "speed": current_speed})
                    except (ValueError, ZeroDivisionError):
                        pass
                elif key == "speed":
                    try:
                        current_speed = float(value.rstrip("x"))
                    except ValueError:
                        pass
                elif key == "progress" and value == "end":
                    on_progress(1.0, current_speed)
        finally:
            if cancelled or self._was_cancelled(job_id):
                _terminate(proc)
                cancelled = True
            try:
                rc = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                _terminate(proc)
                rc = proc.wait(timeout=10)
            err_thread.join(timeout=2)
            with self._lock:
                self._procs.pop(job_id, None)

        if cancelled:
            return rc, "已取消", True
        if rc == 0:
            return 0, "", False
        # Surface the most useful lines: the last real error, plus context.
        tail = [l for l in log if l.strip()]
        err = "\n".join(tail[-6:]) if tail else f"ffmpeg 退出码 {rc}"
        return rc, err, False


def _terminate(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass


def _discard(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
