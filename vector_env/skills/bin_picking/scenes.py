from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
from functools import lru_cache
from pathlib import Path

import numpy as np

from vector_env import __version__

from . import assets, library
from .compose import SceneError, compose, library as assets_library, pile_mode
from .node import sim
from .settings import env_config

KEYS = ("scene_id", "seed", "xml", "meta", "checkpoint", "state_hash", "target", "env")


def default_draw() -> dict:
    return copy.deepcopy(env_config()["draw"])


def _ids(bundle: str, *draw: float) -> tuple[str, ...]:
    _, mt = library._db(Path(bundle))
    return _ids_at(bundle, mt, *draw)


@lru_cache(maxsize=16)
def _ids_at(bundle: str, mtime: int, size_lo: float, size_hi: float, mid: float, m_lo: float,
            m_hi: float) -> tuple[str, ...]:
    con, _ = library._db(Path(bundle))
    rows = con.execute("select id from assets where size_mm between ? and ? and mid_mm >= ? and mass between ? and ? "
                       "order by id", (size_lo, size_hi, mid, m_lo, m_hi)).fetchall()
    if not rows:
        raise SceneError("no library asset matches the draw")
    return tuple(r[0] for r in rows)


@lru_cache(maxsize=16)
def _pile_rows(bundle: str, mtime: int, size_lo: float, size_hi: float, mid: float, thin: float, m_lo: float,
               m_hi: float) -> tuple[tuple[str, float], ...]:
    con, _ = library._db(Path(bundle))
    rows = con.execute("select id, json_extract(row, '$.extents_m') from assets where size_mm between ? and ? and "
                       "mid_mm >= ? and mass between ? and ? order by id", (size_lo, size_hi, mid, m_lo, m_hi)).fetchall()
    T = library.min_sizes()
    out = []
    for aid, ext in rows:
        e = sorted(float(v) for v in json.loads(ext))
        if e[0] * 1000 >= thin and e[2] * 1000 >= T["min_size_mm"] and e[1] * 1000 >= T["min_mid_mm"]:
            out.append((aid, e[0] * e[1] * e[2]))
    if not out:
        raise SceneError("no library asset matches the pile draw")
    return tuple(out)


def pile_draw(draw: dict | None = None) -> dict:
    D = {**default_draw(), **(draw or {})}
    return {**default_draw()["pile"], **(D.get("pile") or {})}


def _pile_settings(seed: int, D: dict, b: str) -> dict:
    """A pile scene: crate, fill level, 1-8 kinds with shares; the pool of copies is sized from the expected count at
    that level (crate floor x level x depth / (pack x mean box volume)), so the rain fill ends at the level with a count
    in the drawn band. A library with fewer kinds than drawn repeats kinds."""
    _, mt = library._db(Path(b))
    rows = _pile_rows(b, mt, *map(float, D["size_mm"]), float(D["mid_mm"]), float(D["thin_mm"]), *map(float, D["mass"]))
    crates = {c["id"]: c for c in assets_library(b)["crates"]}
    rng = np.random.default_rng([int(seed), 0x91E])
    lo, hi = (float(v) for v in D["count"])
    best = None
    for attempt in range(int(D.get("attempts", 24))):
        cid = str(D["crate"][int(rng.integers(len(D["crate"])))]) if isinstance(D["crate"], list) else str(D["crate"])
        c = crates[cid]
        level = float(rng.uniform(*map(float, D["level"])))
        kinds = int(rng.integers(D["kinds"][0], D["kinds"][1] + 1))
        pick = rng.choice(len(rows), kinds, replace=len(rows) < kinds)
        w = rng.dirichlet(np.ones(kinds))
        share: dict[str, float] = {}
        for k, wk in zip(pick, w):
            share[rows[int(k)][0]] = share.get(rows[int(k)][0], 0.0) + float(wk)
        vol = {aid: v for aid, v in rows if aid in share}
        ix, iy = c["inner_half"]
        n_est = 4 * ix * iy * level * (c["rim_z"] - c["floor_z"]) / (float(D["pack"]) * sum(share[a] * vol[a] for a in share))
        miss = max(lo - n_est, n_est - hi, 0.0)
        if best is None or miss < best[0]:
            best = (miss, cid, level, share, n_est)
        if miss == 0:
            break
    _, cid, level, share, n_est = best
    cap = int(D["pool_max"])
    pool = min(cap, int(math.ceil(float(D["pool"]) * n_est)))
    parts = {a: max(1, int(round(pool * share[a]))) for a in sorted(share)}
    while sum(parts.values()) > cap:
        big = max(parts, key=parts.get)
        parts[big] -= 1
    out = {"crate": cid, "mode": "pile", "level": round(level, 4), "parts": parts}
    if float(np.random.default_rng([int(seed), 0xC0FE]).random()) < float(D.get("bury", 0.0)):
        out["bury"] = True
    if D.get("crate_pose"):
        out["crate_pose"] = dict(D["crate_pose"])
    return out


def settings_for(seed: int, draw: dict | None = None, bundle=None) -> dict:
    D = {**default_draw(), **(draw or {})}
    b = str(assets.bundle_dir() if bundle is None else assets.resolve(bundle))
    if D.get("mode") == "pile":
        return _pile_settings(seed, pile_draw(draw), b)
    ids = _ids(b, *map(float, D["size_mm"]), float(D["mid_mm"]), *map(float, D["mass"]))
    rng = np.random.default_rng(int(seed))
    total = int(rng.integers(D["count"][0], D["count"][1] + 1))
    kinds = min(int(rng.integers(D["kinds"][0], min(D["kinds"][1], total) + 1)), len(ids))
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


def _freeze_deep(sc: dict, s: dict, seed: int, bundle) -> tuple[dict, dict]:
    """Second stage of a `rain.freeze_deep` pile: the settled parts again, those with >= freeze_min_depth layers over
    them as static bodies (still drawn and colliding, never moving), the rest placed at their settled poses; settled
    again. Parked pool parts are dropped."""
    R = sc["meta"]["config"]["rain"]
    rows, types = s["parts_state"], [p["type"] for p in sc["meta"]["parts"]]
    hidden = s.get("target") if s.get("target_class") == "hidden" else None
    lay = [{"type": types[r["i"]], "pose": r["pose"], "frozen": r["depth"] >= int(R["freeze_min_depth"]) and r["i"] != hidden}
           for r in rows]
    new = {r["i"]: k for k, r in enumerate(rows)}
    fz = {"parts": lay, "hidden": new.get(hidden, -1),
          "rain": {k: s["fill"].get(k) for k in ("level", "spawned", "ticks")} if s.get("fill") else None}
    sc2 = compose(sc["settings"], seed, bundle, frozen=fz)
    s2 = sim(bundle).call({"op": "settle", "scene": {"scene_id": None, "xml": sc2["xml"], "meta": sc2["meta"]}, "seed": seed})
    if (s2.get("fill") or {}).get("asleep") is False:
        raise SceneError("the frozen pile does not come to rest")
    s2["fill"] = {**(s.get("fill") or {}), "frozen": sum(r["frozen"] for r in lay), "parts": len(lay)}
    return sc2, s2


def env_info(bundle=None) -> dict:
    a = assets.info(bundle)
    return {"version": __version__, "assets_version": a["version"], "assets_sha256": a["sha256"]}


def generate(seed: int, draw: dict | None = None, *, settings: dict | None = None, bundle=None) -> dict:
    bundle = assets.resolve(bundle)
    seed = int(seed)
    sc = _compose(copy.deepcopy(settings) if settings else settings_for(seed, draw, bundle), seed, bundle)
    s = sim(bundle).call({"op": "settle", "scene": {"scene_id": None, "xml": sc["xml"], "meta": sc["meta"]}, "seed": seed})
    if pile_mode(sc["settings"]):
        R = sc["meta"]["config"]["rain"]
        lo, hi = R["count_range"]
        if not lo <= int(s["parts"]) <= hi:
            raise SceneError(f"the pile holds {s['parts']} parts (needs {lo}-{hi})")
        f = s.get("fill") or {}
        if f.get("asleep") is False:
            raise SceneError("the pile does not come to rest")
        if f.get("level") is not None and float(f["level"]) < float(R["level_range"][0]) - float(R["level_tol"]):
            raise SceneError(f"the pile fills {float(f['level']):.2f} of the crate (needs {R['level_range'][0]})")
    if pile_mode(sc["settings"]) and s.get("parts_state"):
        sc, s = _freeze_deep(sc, s, seed, bundle)
    if s.get("target") is None:
        raise SceneError("no part in the bin is visible enough to be a target")
    out = {"scene_id": f"s{seed}-{s['state_hash'][:8]}", "seed": seed, "xml": sc["xml"], "meta": sc["meta"],
           "checkpoint": s["checkpoint"], "state_hash": s["state_hash"], "target": int(s["target"]),
           "env": env_info(bundle)}
    if s.get("target_class") is not None:
        out["target_class"] = s["target_class"]
    if s.get("fill"):
        out["pile"] = {"parts": int(s["parts"]), **s["fill"]}
    return out


def verify(scene: dict, bundle=None) -> bool:
    if not all(k in scene for k in ("xml", "meta", "checkpoint", "state_hash", "target")):
        return False
    try:
        r = sim(assets.resolve(bundle)).call({"op": "verify", "scene": _wire(scene)})
    except Exception:
        return False
    return bool(r.get("ok")) and r.get("state_hash") == scene["state_hash"]


def seed_for(seed: int, index: int, salt: str = "") -> int:
    return int.from_bytes(hashlib.sha256(f"{salt}:{int(seed)}:{int(index)}".encode()).digest()[:6], "big")


SEED_TRIES = 6


def draw_scene(seed0: int, draw: dict | None = None, *, tries: int = SEED_TRIES, generate_fn=None,
               on_error=None, bundle=None) -> tuple[dict | None, Exception | None]:
    gen = generate_fn or generate
    extra = {} if bundle is None else {"bundle": assets.resolve(bundle)}
    err = None
    for k in range(int(tries)):
        try:
            return gen(int(seed0) + k, draw, **extra), None
        except assets.AssetsError:
            raise
        except Exception as e:
            err = e
            if on_error:
                on_error(k, e)
    return None, err


def draw_index(seed: int, index: int, draw: dict | None = None, *, salt: str = "", tries: int = SEED_TRIES,
               generate_fn=None, on_error=None, bundle=None) -> tuple[dict | None, Exception | None]:
    return draw_scene(seed_for(seed, index, salt), draw, tries=tries, generate_fn=generate_fn, on_error=on_error,
                      bundle=bundle)


def _wire(scene: dict) -> dict:
    w = {k: scene.get(k) for k in ("scene_id", "xml", "meta", "checkpoint", "state_hash", "target")}
    if scene.get("target_class") is not None:
        w["target_class"] = scene["target_class"]
    return w


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
