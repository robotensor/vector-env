from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

from .settings import js_dir, node


class SimError(RuntimeError):
    pass


class SimCrashed(SimError):
    pass


def _script(name: str) -> Path:
    js = js_dir()
    path = js / "skills" / "bin-picking" / "bin" / name
    return path if path.exists() else js / "bin" / name


class NodeProc:
    def __init__(self, script: str, bundle: Path, env: dict | None = None):
        self.script = str(_script(script))
        self.bundle = str(bundle)
        self.env = {**os.environ, "VECTOR_ENV_BUNDLE": self.bundle, "VECTOR_SIM_BUNDLE": self.bundle, **(env or {})}
        self.lock = threading.Lock()
        self.p: subprocess.Popen | None = None

    def _start(self) -> subprocess.Popen:
        if self.p is None or self.p.poll() is not None:
            self.p = subprocess.Popen([node(), self.script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=self.env, cwd=str(js_dir()), bufsize=1 << 20)
            threading.Thread(target=self._drain, args=(self.p,), daemon=True).start()
            self.err = []
        return self.p

    def _drain(self, p: subprocess.Popen) -> None:
        for line in p.stderr:
            self.err = (self.err + [line.decode(errors="replace")])[-20:]

    def call(self, msg: dict, timeout: float | None = None) -> dict:
        with self.lock:
            p = self._start()
            timer = threading.Timer(timeout, p.kill) if timeout else None
            if timer:
                timer.start()
            try:
                p.stdin.write((json.dumps(msg, separators=(",", ":"), allow_nan=False) + "\n").encode())
                p.stdin.flush()
                line = p.stdout.readline()
            except (BrokenPipeError, OSError):
                line = b""
            finally:
                if timer:
                    timer.cancel()
            if not line:
                self.close()
                raise SimCrashed(f"{Path(self.script).name} stopped: {''.join(getattr(self, 'err', []))[-800:]}")
            out = json.loads(line)
            if out.get("error") and set(out) <= {"id", "error", "kind"}:
                raise SimError(out["error"])
            return out

    def close(self) -> None:
        p, self.p = self.p, None
        if p is not None:
            try:
                p.stdin.close()
            except OSError:
                pass
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


_local = threading.local()


def sim(bundle: Path) -> NodeProc:
    """This thread's simulator process for one bundle folder (one process per bundle and thread)."""
    procs = getattr(_local, "procs", None)
    if procs is None:
        procs = _local.procs = {}
    key = str(Path(bundle).resolve())
    if key not in procs:
        procs[key] = NodeProc("sim.mjs", Path(key))
    return procs[key]


def close_thread_sims() -> None:
    for p in (getattr(_local, "procs", None) or {}).values():
        p.close()
