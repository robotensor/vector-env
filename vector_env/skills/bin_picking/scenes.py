from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from functools import lru_cache
from pathlib import Path

import numpy as np

from vector_env import __version__

from . import assets, library
from .compose import SceneError, compose
from .node import sim
from .settings import env_config

KEYS = ("scene_id", "seed", "xml", "meta", "checkpoint", "state_hash", "target", "env")


def default_draw() -> dict:
    return copy.deepcopy(env_config()["draw"])


@lru_cache(maxsize=8)
def _ids(bundle: str, size_lo: float, size_hi: float, mid: float, m_lo: float, m_hi: float) -> tuple[str, ...]:
    con, _ = library._db(Path(bundle))
    rows = con.execute("select id from assets where size_mm between ? and ? and mid_mm >= ? and mass between ? and ? "
                       "order by id", (size_lo, size_hi, mid, m_lo, m_hi)).fetchall()
    if not rows:
        raise SceneError("no library asset matches the draw")
    return tuple(r[0] for r in rows)


def settings_for(seed: int, draw: dict | None = None, bundle: Path | None = None) -> dict:
    D = {**default_draw(), **(draw or {})}
    b = str(bundle or assets.bundle_dir())
    ids = _ids(b, *map(float, D["size_mm"]), float(D["mid_mm"]), *map(float, D["mass"]))
    rng = np.random.default_rng(int(seed))
    total = int(rng.integers(D["count"][0], D["count"][1] + 1))
    kinds = int(rng.integers(D["kinds"][0], min(D["kinds"][1], total) + 1))
    chosen = [ids[int(i)] for i in rng.choice(len(ids), kinds, replace=False)]
    counts = np.ones(kinds, int) + rng.multinomial(total - kinds, np.full(kinds, 1.0 / kinds))
    out = {"crate": D["crate"], "parts": {a: int(n) for a, n in zip(chosen, counts)}}
    if D.get("crate_pose"):
        out["crate_pose"] = dict(D["crate_pose"])
    return out


def _compose(settings: dict, seed: int, bundle: Path) -> dict:
    for _ in range(12):
        try:
            return compose(settings, seed, bundle)
        except SceneError:
            if sum(settings["parts"].values()) <= 1:
                raise
            big = max(settings["parts"], key=settings["parts"].get)
            settings["parts"][big] -= 1
            if settings["parts"][big] == 0:
                del settings["parts"][big]
    raise SceneError("could not place the drawn parts")


def env_info(bundle: Path | None = None) -> dict:
    a = assets.info(bundle)
    return {"version": __version__, "assets_version": a["version"], "assets_sha256": a["sha256"]}


def generate(seed: int, draw: dict | None = None, *, settings: dict | None = None) -> dict:
    bundle = assets.ensure()
    seed = int(seed)
    sc = _compose(copy.deepcopy(settings) if settings else settings_for(seed, draw, bundle), seed, bundle)
    s = sim(bundle).call({"op": "settle", "scene": {"scene_id": None, "xml": sc["xml"], "meta": sc["meta"]}, "seed": seed})
    if s.get("target") is None:
        raise SceneError("no part in the bin is visible enough to be a target")
    return {"scene_id": f"s{seed}-{s['state_hash'][:8]}", "seed": seed, "xml": sc["xml"], "meta": sc["meta"],
            "checkpoint": s["checkpoint"], "state_hash": s["state_hash"], "target": int(s["target"]),
            "env": env_info(bundle)}


def verify(scene: dict) -> bool:
    if not all(k in scene for k in ("xml", "meta", "checkpoint", "state_hash", "target")):
        return False
    try:
        r = sim(assets.ensure()).call({"op": "verify", "scene": _wire(scene)})
    except Exception:
        return False
    return bool(r.get("ok")) and r.get("state_hash") == scene["state_hash"]


def seed_for(seed: int, index: int, salt: str = "") -> int:
    return int.from_bytes(hashlib.sha256(f"{salt}:{int(seed)}:{int(index)}".encode()).digest()[:6], "big")


SEED_TRIES = 6


def draw_scene(seed0: int, draw: dict | None = None, *, tries: int = SEED_TRIES, generate_fn=None,
               on_error=None) -> tuple[dict | None, Exception | None]:
    gen = generate_fn or generate
    err = None
    for k in range(int(tries)):
        try:
            return gen(int(seed0) + k, draw), None
        except assets.AssetsError:
            raise
        except Exception as e:
            err = e
            if on_error:
                on_error(k, e)
    return None, err


def draw_index(seed: int, index: int, draw: dict | None = None, *, salt: str = "", tries: int = SEED_TRIES,
               generate_fn=None, on_error=None) -> tuple[dict | None, Exception | None]:
    return draw_scene(seed_for(seed, index, salt), draw, tries=tries, generate_fn=generate_fn, on_error=on_error)


def _wire(scene: dict) -> dict:
    return {k: scene.get(k) for k in ("scene_id", "xml", "meta", "checkpoint", "state_hash", "target")}


def scene_cache_root(path: str | Path | None = None) -> Path | None:
    p = os.environ.get("VECTOR_ENV_SCENE_CACHE") if path is None else str(path)
    if p is None:
        return Path.home() / ".cache" / "vector-env" / "scenes"
    if p.strip().lower() in ("", "off"):
        return None
    return Path(p).expanduser()


def draw_key(draw: dict | None = None) -> str:
    D = {**default_draw(), **(draw or {})}
    return hashlib.sha256(json.dumps(D, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


class SceneCache:
    def __init__(self, root: str | Path, env: dict, draw: dict | None = None):
        self.env = {k: env.get(k) for k in ("version", "assets_version", "assets_sha256")}
        self.dir = Path(root) / str(self.env["version"]) / str(self.env["assets_sha256"]) / draw_key(draw)

    def path(self, seed: int, index: int) -> Path:
        return self.dir / str(int(seed)) / f"{int(index)}.json"

    def get(self, seed: int, index: int, salt: str = "", tries: int = SEED_TRIES):
        try:
            x = json.loads(self.path(seed, index).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(x, dict):
            return None
        if x.get("skipped") is True and isinstance(x.get("error"), str):
            return None, SceneError(x["error"])
        env = x.get("env")
        if not all(k in x for k in KEYS) or not isinstance(env, dict) or {k: env.get(k) for k in self.env} != self.env:
            return None
        if not 0 <= int(x["seed"]) - seed_for(seed, index, salt) < tries:
            return None
        return x, None

    def put(self, seed: int, index: int, scene: dict | None, err: Exception | None) -> bool:
        if scene is None and not isinstance(err, SceneError):
            return False
        if scene is not None and (not all(k in scene for k in KEYS) or not isinstance(scene.get("env"), dict)
                                  or {k: scene["env"].get(k) for k in self.env} != self.env):
            return False
        doc = scene if scene is not None else {"skipped": True, "error": str(err)}
        p = self.path(seed, index)
        tmp = None
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(doc, f, separators=(",", ":"))
            os.replace(tmp, p)
        except OSError:
            if tmp:
                Path(tmp).unlink(missing_ok=True)
            return False
        return True
