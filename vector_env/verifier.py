from __future__ import annotations

import base64
import os
import queue
import struct
import threading
from pathlib import Path

from . import assets
from .node import NodeProc

FORMAT = "vector-verify/1"
CHAIN_FORMAT = "vector-console/1"
CTRL = 7
FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3
MASK = 0xFFFFFFFFFFFFFFFF
SCENE_KEYS = ("scene_id", "xml", "meta", "checkpoint", "state_hash", "target")


class VerifyError(RuntimeError):
    pass


def fnv1a64(data: bytes, h: int = FNV_OFFSET) -> int:
    for b in data:
        h = ((h ^ b) * FNV_PRIME) & MASK
    return h


def _hex(h: int) -> str:
    return f"{h:016x}"


def row_bytes(rows) -> bytes:
    if isinstance(rows, (bytes, bytearray)):
        b = bytes(rows)
    elif isinstance(rows, str):
        b = base64.b64decode(rows, validate=True)
    else:
        flat = [float(x) for r in rows for x in r] if rows and isinstance(rows[0], (list, tuple)) else [float(x) for x in rows]
        b = struct.pack(f"<{len(flat)}d", *flat)
    if len(b) % (8 * CTRL):
        raise VerifyError(f"rows of {len(b)} bytes are not whole rows of {CTRL} float64")
    vals = struct.unpack(f"<{len(b) // 8}d", b)
    if any(v != v or v in (float("inf"), float("-inf")) for v in vals):
        raise VerifyError("rows hold a non-finite value")
    return struct.pack(f"<{len(vals)}d", *(v + 0.0 for v in vals))


def rows_hash(rows, h: str | None = None) -> str:
    return _hex(fnv1a64(row_bytes(rows), int(h, 16) if h else FNV_OFFSET))


def chain_seed(nonce: str, session: str, scene_id: str) -> str:
    return _hex(fnv1a64(f"{CHAIN_FORMAT}:{nonce}:{session}:{scene_id}".encode()))


def chain_next(prev: str, k: int, a: int, b: int, sh: str, rows) -> str:
    return _hex(fnv1a64(row_bytes(rows), fnv1a64(f"{prev}:{int(k)}:{int(a)}:{int(b)}:{sh}:".encode())))


class Workers:
    def __init__(self, size: int | None = None, bundle: Path | None = None):
        self.size = max(1, int(size or os.environ.get("VECTOR_ENV_VERIFIERS") or 1))
        self.bundle = bundle
        self.free: queue.Queue = queue.Queue()
        self.made = 0
        self.lock = threading.Lock()
        self.all: list[NodeProc] = []

    def _take(self) -> NodeProc:
        try:
            return self.free.get_nowait()
        except queue.Empty:
            pass
        with self.lock:
            if self.made < self.size:
                self.made += 1
                p = NodeProc("verifier.mjs", self.bundle or assets.ensure())
                self.all.append(p)
                return p
        return self.free.get()

    def call(self, msg: dict, timeout: float | None = 120) -> dict:
        p = self._take()
        try:
            r = p.call(msg, timeout=timeout)
        finally:
            self.free.put(p)
        r.pop("id", None)
        return r

    def close(self) -> None:
        for p in self.all:
            p.close()


_workers: Workers | None = None
_workers_lock = threading.Lock()


def workers() -> Workers:
    global _workers
    with _workers_lock:
        if _workers is None:
            _workers = Workers()
        return _workers


def reset(p: Workers | None = None) -> None:
    global _workers
    with _workers_lock:
        old, _workers = _workers, p
    if old is not None and old is not p:
        old.close()


def _wire(scene: dict) -> dict:
    missing = [k for k in SCENE_KEYS if k != "scene_id" and k not in scene]
    if missing:
        raise VerifyError(f"the scene lacks {', '.join(missing)}")
    return {k: scene.get(k) for k in SCENE_KEYS}


def _b64(rows) -> str:
    return base64.b64encode(row_bytes(rows)).decode()


def verify(scene: dict, rows, *, time_limit_s: float, hold: bool = False, claim: dict | None = None, every: int = 1,
           marks: bool = False, timeout: float | None = 120) -> dict:
    msg = {"op": "verify", "scene": _wire(scene), "rows": _b64(rows), "time_limit_s": float(time_limit_s),
           "hold": bool(hold), "every": int(every), "marks": bool(marks)}
    if claim:
        msg["claim"] = claim
    return workers().call(msg, timeout=timeout)


def verify_result(scene: dict, result: dict, *, time_limit_s: float) -> dict:
    claim = {"outcome": result["outcome"]} if result.get("outcome") else {}
    if result.get("action_hash") and result.get("rows_sent") is not None and result.get("rows_sent") == result.get("n"):
        claim["action_hash"] = result["action_hash"]
    return verify(scene, result.get("ctrl") or "", time_limit_s=time_limit_s, hold=True, claim=claim or None, every=0)


def probe(scene: dict, ticks: int = 40, every: int = 5) -> dict:
    r = workers().call({"op": "probe", "scene": _wire(scene), "ticks": int(ticks), "every": int(every)})
    if not r.get("ok"):
        raise VerifyError(r.get("detail") or r.get("reason") or "probe failed")
    r.pop("ok", None)
    return r


def prepare(scene: dict) -> bool:
    return bool(workers().call({"op": "prepare", "scene": _wire(scene)}).get("ok"))


def pin() -> dict:
    return workers().call({"op": "pin"})
