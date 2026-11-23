from __future__ import annotations

import copy
import hashlib
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import __version__, assets, library
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
