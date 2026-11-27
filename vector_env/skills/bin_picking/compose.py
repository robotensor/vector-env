from __future__ import annotations

import copy
import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path

import mujoco
import numpy as np

from . import library as assets_index
from .assets import bundle_dir
from .settings import cell_config, env_config


class SceneError(ValueError):
    pass


def config() -> dict:
    return cell_config()


@lru_cache(None)
def _library(bundle: str) -> dict:
    lib = json.loads((Path(bundle) / "library.json").read_text())
    for c in lib["crates"]:
        c["kind"] = "bin"
        c["max_parts"] = max_parts(c, config())
    return lib


def max_parts(crate: dict, cfg: dict) -> int:
    F, L = cfg["fill"], cfg["limits"]
    ix, iy = crate["inner_half"]
    a = math.pi * (float(F["ref_radius"]) + float(F["spacing"]) / 2) ** 2
    return int(min(L["parts_total"][1], math.floor(float(L["floor_fill_max"]) * 4 * ix * iy / a)))


def library(bundle: Path | None = None) -> dict:
    return _library(str(bundle or bundle_dir()))


def bundle_hash(bundle: Path | None = None) -> str:
    return json.loads(((bundle or bundle_dir()) / "manifest.json").read_text())["bundle"]


def asset(aid: str, bundle: Path | None = None) -> dict | None:
    return assets_index.get(Path(bundle or bundle_dir()), aid)


def stale_files(meta: dict, bundle: Path | None = None) -> list[str]:
    want = meta.get("file_sha256") or {}
    have = {}
    for aid in meta.get("part_types") or {}:
        r = asset(aid, bundle)
        if r is None:
            continue
        sha = r.get("sha256") or {}
        files = r["files"]
        if sha:
            have[files["ray"]] = sha["ray"]
            have.update(zip(files["hulls"], sha["hulls"]))
    return sorted(p for p, h in want.items() if have.get(p) != h)


def _yaw_quat(yaw: float) -> list[float]:
    return [math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2)]


def _rand_quat(rng: np.random.Generator) -> list[float]:
    q = rng.normal(size=4)
    q /= np.linalg.norm(q)
    return [float(x) for x in q]


JOINT_RANGE = [(-6.28319, 6.28319)] * 2 + [(-3.1415, 3.1415)] + [(-6.28319, 6.28319)] * 3
HOME_Q = [2.8197, -1.8488, 1.7888, -1.5108, -1.5708, -1.8927]


def default_robot_q() -> list[float]:
    return list(HOME_Q)


PILE = "pile"


def container(settings: dict, lib: dict, cfg: dict) -> dict:
    cid = settings.get("crate") or cfg["cell"]["default_crate"]
    if cid == PILE and cfg["cell"].get("pile_enabled"):
        L, Pc = cfg["limits"], cfg["pile"]
        half = settings.get("pile_half")
        if half is None:
            half = Pc["half"]
        try:
            hx, hy = (float(v) for v in half)
        except (TypeError, ValueError):
            raise SceneError("pile_half must be [hx, hy] in m") from None
        lo, hi = Pc["half_range"]
        if not (lo <= hx <= hi and lo <= hy <= hi):
            raise SceneError(f"pile_half: each half size {lo}-{hi} m")
        return {"id": PILE, "kind": "pile", "label": "Pile (no bin)", "outer_half": [hx, hy], "inner_half": [hx, hy],
                "floor_z": 0.0, "rim_z": float(Pc["lift_base"]), "visual": None, "scale": [1.0, 1.0, 1.0]}
    crates = {c["id"]: c for c in lib["crates"]}
    crate = crates.get(cid)
    if crate is None:
        raise SceneError(f"unknown crate {cid!r}; choose one of {sorted(crates)}")
    return {**crate, "kind": "bin", "max_parts": max_parts(crate, cfg)}


def footprint_r(extents) -> float:
    e = sorted(float(v) for v in extents)
    return 0.5 * math.hypot(e[1], e[2])


def capacity_error(area: float, n: int, floor: float, cfg: dict, crate: dict) -> str:
    fit = max(1, math.floor(float(cfg["limits"]["floor_fill_max"]) * floor / (area / max(n, 1))))
    return (f"at most {fit} parts of these sizes fit in this crate ({crate['id']}: on its floor, a few partly "
            f"overlapping, at most {cfg['fill'].get('max_layers', 1)} layers; asked for {n})")


def pile_mode(settings: dict) -> bool:
    return isinstance(settings, dict) and settings.get("mode") == PILE


def mode_config(cfg: dict, pile: bool) -> dict:
    """The cell config of a scene: pile scenes (rain fill) take their limits and physics from `rain:`."""
    if not pile:
        return cfg
    R = cfg["rain"]
    out = dict(cfg)
    out["limits"] = {**cfg["limits"], **(R.get("limits") or {})}
    out["physics"] = {**cfg["physics"], **(R.get("physics") or {})}
    T = (env_config().get("task") or {}).get("pile") or {}
    out["rules"] = {**cfg["rules"], "mode": PILE, "try_s": float(T.get("time_limit_s", 15.0)),
                    "picks": int(T.get("picks", 10))}
    return out


def validate(settings: dict, lib: dict, cfg: dict, bundle: Path | None = None, seed: int = 0, layout: bool = False):
    bundle = Path(bundle or bundle_dir())
    if not isinstance(settings, dict):
        raise SceneError("settings must be an object")
    rain = pile_mode(settings)
    if settings.get("mode") not in (None, PILE):
        raise SceneError("mode must be 'pile' or left out")
    cfg = mode_config(cfg, rain)
    L = cfg["limits"]
    crate = container(settings, lib, cfg)
    pile = crate["kind"] == "pile"
    if rain and pile:
        raise SceneError("a pile scene needs a crate")
    what = "the pile zone" if pile else "the crate"
    pose = settings.get("crate_pose") or {}
    dflt = cfg["pile"]["pose"] if pile else {"x": 0.75, "y": 0.0, "yaw_deg": 0.0}
    x, y, yaw = (float(pose.get("x", dflt["x"])), float(pose.get("y", dflt["y"])), float(pose.get("yaw_deg", dflt["yaw_deg"])))
    if not all(map(math.isfinite, (x, y, yaw))):
        raise SceneError("crate_pose must be numbers")
    ox, oy = crate["outer_half"]
    c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    T = cfg["cell"]["table"]
    corners = [(x + c * sx * ox - s * sy * oy, y + s * sx * ox + c * sy * oy) for sx in (-1, 1) for sy in (-1, 1)]
    if pile:
        for px, py in corners:
            if not (T["x"][0] <= px <= T["x"][1] and T["y"][0] <= py <= T["y"][1]):
                raise SceneError(f"{what} must be fully on the table")
        lx, ly = c * -x + s * -y, -s * -x + c * -y
        k = float(cfg["pile"]["base_keepout_r"])
        if math.hypot(max(abs(lx) - ox, 0.0), max(abs(ly) - oy, 0.0)) < k:
            raise SceneError(f"{what} must stay {k:.2f} m clear of the robot base axis")
    else:
        r, bearing = math.hypot(x, y), math.degrees(math.atan2(y, x))
        if not L["crate_r"][0] <= r <= L["crate_r"][1]:
            raise SceneError(f"crate centre must be {L['crate_r'][0]}-{L['crate_r'][1]} m from the robot (is {r:.2f})")
        if abs(bearing) > L["crate_bearing_deg"]:
            raise SceneError(f"crate must be within +-{L['crate_bearing_deg']} deg of the robot's front")
        for px, py in corners:
            if math.hypot(px, py) > L["corner_r_max"]:
                raise SceneError("a crate corner is out of the robot's reach")
            if not (T["x"][0] <= px <= T["x"][1] and T["y"][0] <= py <= T["y"][1]):
                raise SceneError("the crate must stand on the table")
            if math.hypot(px, py) < 0.25:
                raise SceneError("the crate overlaps the robot base")
    if layout:
        counts, rows, groups, expansion = {}, {}, [], []
    else:
        counts, rows = _parts(settings, cfg, bundle)
        groups, expansion = _random(settings, cfg, bundle, seed, rows)
    for g in expansion:
        for aid, n in g["chosen"].items():
            counts[aid] = counts.get(aid, 0) + n
    for aid, n in counts.items():
        if n > L["per_type"][1]:
            raise SceneError(f"{aid}: {L['per_type'][0]}-{L['per_type'][1]} copies per part (is {n}, with the random draw)")
    total = sum(counts.values())
    if not layout and not L["parts_total"][0] <= total <= L["parts_total"][1]:
        raise SceneError(f"{L['parts_total'][0]}-{L['parts_total'][1]} parts in total (is {total})")
    ix, iy = crate["inner_half"]
    if pile:
        vol = 4 * ix * iy * float(cfg["pile"]["fill_height"])
        used = sum(n * float(np.prod(rows[k]["extents_m"])) for k, n in counts.items())
        if used > float(cfg["pile"]["fill_ratio_max"]) * vol:
            raise SceneError(f"too many parts for {what} ({used / vol:.0%} of its volume)")
    elif counts and not rain:
        sp = float(cfg["fill"]["spacing"]) / 2
        area = sum(n * math.pi * (footprint_r(rows[k]["extents_m"]) + sp) ** 2 for k, n in counts.items())
        floor = 4 * ix * iy
        if area > float(L["floor_fill_max"]) * floor:
            raise SceneError(capacity_error(area, total, floor, cfg, crate))
    q = settings.get("robot_q") or default_robot_q()
    if len(q) != 6:
        raise SceneError("robot_q needs 6 joint angles")
    m = float(L["robot_q_margin"])
    for k, (v, (lo, hi)) in enumerate(zip(q, JOINT_RANGE)):
        if not lo + m <= float(v) <= hi - m:
            raise SceneError(f"robot joint {k + 1} must be within [{lo + m:.2f}, {hi - m:.2f}] rad")
    st = {"crate": crate["id"], "crate_pose": {"x": x, "y": y, "yaw_deg": yaw},
          "parts": {} if layout else dict(sorted((k, n) for k, n in _explicit(settings).items() if n)),
          "robot_q": [float(v) for v in q]}
    if pile:
        st["pile_half"] = list(crate["inner_half"])
    if rain:
        lo, hi = cfg["rain"]["level_range"]
        try:
            level = float(settings.get("level", cfg["rain"]["level"]))
        except (TypeError, ValueError):
            raise SceneError("level must be a number") from None
        if not lo <= level <= hi:
            raise SceneError(f"level: the pile fills {lo:g}-{hi:g} of the crate depth")
        st["mode"], st["level"] = PILE, level
        if settings.get("bury"):
            st["bury"] = True
    if groups:
        st["random"] = groups
    return st, dict(sorted(counts.items())), rows, expansion


def _explicit(settings: dict) -> dict:
    parts = settings.get("parts") or {}
    if not isinstance(parts, dict):
        raise SceneError("parts must be an object {asset_id: count}")
    out = {}
    for k, n in parts.items():
        try:
            out[str(k)] = int(n)
        except (TypeError, ValueError):
            raise SceneError(f"{k}: the count must be a whole number") from None
    return out


def _parts(settings: dict, cfg: dict, bundle: Path) -> tuple[dict, dict]:
    L = cfg["limits"]
    parts = _explicit(settings)
    if len(parts) > L["parts_total"][1]:
        raise SceneError(f"at most {L['parts_total'][1]} different parts")
    counts, rows = {}, {}
    for k, n in parts.items():
        if not L["per_type"][0] <= n <= L["per_type"][1]:
            raise SceneError(f"{k}: {L['per_type'][0]}-{L['per_type'][1]} copies per part")
        r = asset(k, bundle)
        if r is None:
            raise SceneError(f"unknown part {k!r}")
        small = assets_index.too_small(r)
        if small:
            raise SceneError(f"{k}: too small for the cell ({small}); the library offers only parts the gripper and "
                             f"the camera can handle")
        if n:
            counts[k], rows[k] = n, r
    return counts, rows


FILTER_KEYS = {"source", "class", "material", "size_mm", "mass", "q"}


def _random(settings: dict, cfg: dict, bundle: Path, seed: int, rows: dict) -> tuple[list, list]:
    L = cfg["limits"]
    groups = settings.get("random") or []
    if not isinstance(groups, list):
        raise SceneError("random must be a list of {filter, kinds, count}")
    if len(groups) > int(L.get("random_groups", 8)):
        raise SceneError(f"at most {L.get('random_groups', 8)} random groups")
    norm, out = [], []
    for gi, g in enumerate(groups):
        if not isinstance(g, dict):
            raise SceneError("random must be a list of {filter, kinds, count}")
        flt = g.get("filter") or {}
        if not isinstance(flt, dict) or set(flt) - FILTER_KEYS:
            raise SceneError(f"random group {gi + 1}: filter keys are {sorted(FILTER_KEYS)}")
        f = {}
        for k in ("source", "class", "material"):
            v = flt.get(k)
            if v not in (None, "", []):
                f[k] = sorted(str(x) for x in v) if isinstance(v, list) else str(v)
        for k in ("size_mm", "mass"):
            v = flt.get(k)
            if v is not None:
                if not isinstance(v, (list, tuple)) or len(v) != 2:
                    raise SceneError(f"random group {gi + 1}: {k} must be [lo, hi]")
                try:
                    f[k] = [None if x is None else float(x) for x in v]
                except (TypeError, ValueError):
                    raise SceneError(f"random group {gi + 1}: {k} must be numbers") from None
        if flt.get("q"):
            f["q"] = str(flt["q"])[:100]
        try:
            count, kinds = int(g.get("count", 0)), int(g.get("kinds", 1))
        except (TypeError, ValueError):
            raise SceneError(f"random group {gi + 1}: kinds and count must be whole numbers") from None
        if not 1 <= count <= L["parts_total"][1]:
            raise SceneError(f"random group {gi + 1}: count must be 1-{L['parts_total'][1]}")
        if not 1 <= kinds <= count:
            raise SceneError(f"random group {gi + 1}: kinds must be 1-{count} (at least one copy of each)")
        candidates = [i for i in assets_index.ids(bundle, f) if i not in rows]
        if len(candidates) < kinds:
            raise SceneError(f"random group {gi + 1}: only {len(candidates)} parts match its filter (kinds: {kinds})")
        rng = np.random.default_rng([int(seed), 0xA55E7, gi])
        pick = [candidates[int(i)] for i in rng.choice(len(candidates), size=kinds, replace=False)]
        chosen = {aid: count // kinds + (1 if k < count % kinds else 0) for k, aid in enumerate(pick)}
        for aid in pick:
            rows[aid] = asset(aid, bundle)
        norm.append({"filter": f, "kinds": kinds, "count": count})
        out.append({"filter": f, "kinds": kinds, "count": count, "chosen": chosen})
    return norm, out


def compose(settings: dict, seed: int, bundle: Path | None = None, layout: bool = False, frozen: dict | None = None) -> dict:
    """`frozen` (second stage of a freeze_deep pile, see scenes.generate): {"parts": [{type, pose, frozen}], "hidden":
    index or -1} — the settled parts in this order, frozen ones as static bodies at their pose, the others placed at
    their pose by a `place` fill."""
    bundle = Path(bundle or bundle_dir()).resolve()
    lib, cfg = library(bundle), config()
    if frozen is None:
        st, counts, parts, expansion = validate(settings, lib, cfg, bundle, seed, layout=layout)
    else:
        st, expansion = copy.deepcopy(settings), []
        counts, parts = {}, {}
        for r in frozen["parts"]:
            counts[r["type"]] = counts.get(r["type"], 0) + 1
            parts[r["type"]] = asset(r["type"], bundle)
    cfg = mode_config(cfg, pile_mode(st))
    P, C = cfg["physics"], cfg["cell"]
    crate = container(st, lib, cfg)

    spec = mujoco.MjSpec.from_string((bundle / lib["robot"]["xml"]).read_text())
    spec.meshdir = str(bundle) + "/"
    spec.modelname = "vector_cell"
    o = spec.option
    o.timestep = P["timestep"]
    o.integrator = getattr(mujoco.mjtIntegrator, f"mjINT_{P['integrator'].upper()}")
    o.cone = getattr(mujoco.mjtCone, f"mjCONE_{P['cone'].upper()}")
    o.impratio = P["impratio"]
    o.iterations = P["iterations"]
    o.ls_iterations = P["ls_iterations"]
    if P.get("tolerance") is not None:
        o.tolerance = float(P["tolerance"])
    if P.get("ls_tolerance") is not None:
        o.ls_tolerance = float(P["ls_tolerance"])
    o.noslip_iterations = int(P.get("noslip_iterations", 0))
    if P.get("solver"):
        o.solver = getattr(mujoco.mjtSolver, f"mjSOL_{P['solver'].upper()}")
    if P.get("multiccd") is False:
        o.disableflags |= int(mujoco.mjtDisableBit.mjDSBL_MULTICCD)
    if P.get("sleep"):
        o.enableflags |= int(mujoco.mjtEnableBit.mjENBL_SLEEP)
        o.sleep_tolerance = float(P["sleep_tolerance"])
    spec.visual.global_.offwidth = 640
    spec.visual.global_.offheight = 480
    G = mujoco.mjtGeom
    w = spec.worldbody
    tx, ty = C["table"]["x"], C["table"]["y"]
    w.add_geom(name="table", type=G.mjGEOM_PLANE, size=[(tx[1] - tx[0]) / 2, (ty[1] - ty[0]) / 2, 0.1],
               pos=[(tx[0] + tx[1]) / 2, (ty[0] + ty[1]) / 2, 0.0], rgba=[0.55, 0.55, 0.56, 1])
    w.add_light(pos=[0.5, 0, 3.0], dir=[0, 0, -1], diffuse=[0.7, 0.7, 0.7])

    cp = st["crate_pose"]
    yaw = math.radians(cp["yaw_deg"])
    if crate["kind"] == "bin":
        (ox, oy), (ix, iy), fz, rz = crate["outer_half"], crate["inner_half"], crate["floor_z"], crate["rim_z"]
        cb = w.add_body(name="crate", pos=[cp["x"], cp["y"], -fz], quat=_yaw_quat(yaw))
        rgba = [0.8, 0.7, 0.2, 1]
        tw_x, tw_y = ox - ix, oy - iy
        h = (rz - fz) / 2
        for nm, sz, ps in (("crate_wall_px", [tw_x / 2, oy, h], [ix + tw_x / 2, 0, fz + h]),
                           ("crate_wall_nx", [tw_x / 2, oy, h], [-ix - tw_x / 2, 0, fz + h]),
                           ("crate_wall_py", [ix, tw_y / 2, h], [0, iy + tw_y / 2, fz + h]),
                           ("crate_wall_ny", [ix, tw_y / 2, h], [0, -iy - tw_y / 2, fz + h])):
            cb.add_geom(name=nm, type=G.mjGEOM_BOX, size=sz, pos=ps, rgba=rgba)
        if P.get("crate_floor"):
            t = float(P["crate_floor"]) / 2
            cb.add_geom(name="crate_floor", type=G.mjGEOM_BOX, size=[ix, iy, t], pos=[0, 0, fz - t], rgba=rgba)

    meshes, file_sha = {}, {}
    for pid in counts:
        p = parts[pid]
        f = p["files"]
        if f["ray"] not in meshes:
            g = meshes[f["ray"]] = f"m{len(meshes)}"
            spec.add_mesh(name=f"{g}_ray", file=f["ray"])
            for k, hf in enumerate(f["hulls"]):
                spec.add_mesh(name=f"{g}_hull{k}", file=hf, maxhullvert=int(P["maxhullvert"]))
        sha = p.get("sha256") or {}
        if sha:
            file_sha[f["ray"]] = sha["ray"]
            file_sha.update(zip(f["hulls"], sha["hulls"]))
    instances = ([r["type"] for r in frozen["parts"]] if frozen is not None
                 else [pid for pid, n in counts.items() for _ in range(n)])
    still = [bool(r["frozen"]) for r in frozen["parts"]] if frozen is not None else [False] * len(instances)
    pk = C["park"]
    ws = float(P["friction_static_weight"])
    for i, pid in enumerate(instances):
        p = parts[pid]
        g = meshes[p["files"]["ray"]]
        mu = ws * float(p["mu_static"]) + (1.0 - ws) * float(p["mu_dynamic"])
        if still[i]:
            pose = frozen["parts"][i]["pose"]
            b = w.add_body(name=f"part{i}", pos=pose[:3], quat=pose[3:])
        else:
            b = w.add_body(name=f"part{i}", pos=[pk["x"] - pk["dx"] * i, 0.0, pk["z"]], gravcomp=1.0)
        _part_body(b, i, p, g, mu, P, G, still[i])
    files = sorted({m.file for m in spec.meshes})

    model = spec.compile()
    xml = spec.to_xml().replace(f' meshdir="{spec.meshdir}"', "")
    meta = _meta(model, st, crate, instances, parts, cfg, lib, seed, frozen)
    meta.update(bundle=bundle_hash(bundle), counts=counts, random=expansion, files=files, file_sha256=file_sha,
                xml_sha256=hashlib.sha256(xml.encode()).hexdigest())
    if layout:
        meta["layout"] = True
    return {"xml": xml, "meta": meta, "settings": st}


def _part_body(b, i, p, g, mu, P, G, still: bool) -> None:
    if not still:
        fj = b.add_freejoint(name=f"part{i}_free")
        if P.get("part_damping"):
            fj.damping = [float(P["part_damping"]), 0.0, 0.0]
        if P.get("part_armature"):
            fj.armature = float(P["part_armature"])
        b.explicitinertial = True
        i_min = float(p["mass"]) * float(P.get("min_inertia_radius", 0.006)) ** 2
        b.mass, b.ipos, b.iquat = p["mass"], p["com"], p["inertia_quat"]
        b.inertia = [max(float(v), i_min) for v in p["inertia"]]
    b.add_geom(name=f"part{i}_ray", type=G.mjGEOM_MESH, meshname=f"{g}_ray", group=2, contype=0,
               conaffinity=0, mass=0, rgba=[0.7, 0.7, 0.7, 1])
    for k in range(p["hulls"]):
        b.add_geom(name=f"part{i}_hull{k}", type=G.mjGEOM_MESH, meshname=f"{g}_hull{k}", group=3, contype=1,
                   conaffinity=1, mass=0, condim=int(P["part_condim"]), priority=int(P["part_priority"]),
                   friction=[mu, 0.005, 0.0001], solref=P["part_solref"], solimp=P["part_solimp"],
                   margin=float(P.get("part_margin", 0.0)), gap=float(P.get("part_gap", 0.0)))


def _id(m, kind, name) -> int:
    i = mujoco.mj_name2id(m, kind, name)
    if i < 0:
        raise KeyError(name)
    return int(i)


def _meta(m, st, crate, instances, parts, cfg, lib, seed, frozen=None) -> dict:
    O = mujoco.mjtObj
    R = lib["robot"]
    arm_j = [_id(m, O.mjOBJ_JOINT, n) for n in R["arm_joints"]]
    arm_act = [int(np.flatnonzero((m.actuator_trntype == 0) & (m.actuator_trnid[:, 0] == j))[0]) for j in arm_j]
    grip_act = _id(m, O.mjOBJ_ACTUATOR, R["gripper_actuator"])
    grip_bodies = set()
    gb = _id(m, O.mjOBJ_BODY, R["gripper_base"])
    for b in range(m.nbody):
        a = b
        while a > 0 and a != gb:
            a = int(m.body_parentid[a])
        if a == gb:
            grip_bodies.add(b)
    pad = {side: [_id(m, O.mjOBJ_BODY, n) for n in names] for side, names in R["pads"].items()}
    robot_bodies = [b for b in range(1, m.nbody) if not m.body(b).name.startswith(("part", "crate"))]
    part_rows = []
    for i, pid in enumerate(instances):
        b = _id(m, O.mjOBJ_BODY, f"part{i}")
        hulls = [_id(m, O.mjOBJ_GEOM, f"part{i}_hull{k}") for k in range(parts[pid]["hulls"])]
        j = int(m.body_jntadr[b])
        row = {"i": i, "type": pid, "body": b, "qpos": int(m.jnt_qposadr[j]) if j >= 0 else -1,
               "dof": int(m.jnt_dofadr[j]) if j >= 0 else -1, "ray_geom": _id(m, O.mjOBJ_GEOM, f"part{i}_ray"),
               "hull_geoms": hulls, "radius": parts[pid]["radius"]}
        if j < 0:
            row["frozen"] = True
        part_rows.append(row)
    cp = st["crate_pose"]
    yaw = math.radians(cp["yaw_deg"])
    nsub = int(round(1.0 / cfg["control"]["hz"] / cfg["physics"]["timestep"]))
    return {
        "format": "vector-scene/1", "seed": int(seed), "settings": st,
        "nq": int(m.nq), "nv": int(m.nv), "nu": int(m.nu), "nbody": int(m.nbody), "ngeom": int(m.ngeom),
        "substeps": nsub, "tick_s": nsub * float(m.opt.timestep),
        "robot": {"arm_joints_qpos": [int(m.jnt_qposadr[j]) for j in arm_j], "arm_dofs": [int(m.jnt_dofadr[j]) for j in arm_j],
                  "arm_actuators": arm_act, "gripper_actuator": grip_act,
                  "driver_qpos": int(m.jnt_qposadr[_id(m, O.mjOBJ_JOINT, R["driver_joint"])]),
                  "tcp_site": _id(m, O.mjOBJ_SITE, R["tcp_site"]),
                  "joint_range": [m.jnt_range[j].tolist() for j in arm_j],
                  "gripper_bodies": sorted(grip_bodies), "robot_bodies": robot_bodies,
                  "pad_left": pad["left"], "pad_right": pad["right"], "q0": st["robot_q"]},
        "crate": {**{k: crate[k] for k in ("id", "kind", "label", "outer_half", "inner_half", "visual", "scale")},
                  **({"max_parts": crate["max_parts"]} if "max_parts" in crate else {}),
                  "floor_z": 0.0, "rim_z": crate["rim_z"] - crate["floor_z"],
                  "pos": [cp["x"], cp["y"], 0.0 - crate["floor_z"]], "yaw": yaw, "quat": _yaw_quat(yaw)},
        "parts": part_rows,
        "part_types": {pid: _part_type(parts[pid]) for pid in dict.fromkeys(instances)},
        "fill": _frozen_fill(frozen, cfg) if frozen is not None
                else _rain(m, st, crate, part_rows, instances, parts, cfg, seed) if pile_mode(st)
                else _spawns(st, crate, instances, parts, cfg, seed) if crate["kind"] == "pile"
                else _place(m, st, crate, part_rows, cfg, seed,
                            {"qpos": [int(m.jnt_qposadr[j]) for j in arm_j], "act": arm_act, "grip": grip_act,
                             "q0": [float(v) for v in st["robot_q"]], "nsub": nsub}),
        "camera": _camera(st, crate, cfg),
        "config": {k: copy.deepcopy(cfg[k]) for k in ("physics", "control", "fill", "rules", "camera", "cell", "pile")
                   + (("rain",) if pile_mode(st) else ())},
    }


def _part_type(r: dict) -> dict:
    return {"label": r["label"], "source": r["source"], "class": r.get("class"), "material": r.get("material"),
            "surface": r.get("surface"), "visual": r["files"]["visual"], "visual_offset": r.get("visual_offset") or [0, 0, 0],
            "thumb": r.get("thumb"), "look": r.get("look"), "extents": r["extents_m"], "mass": r["mass"]}


def _spawns(st, crate, instances, parts, cfg, seed) -> dict:
    F = cfg["fill"]
    rng = np.random.default_rng([int(seed), 0x5EED])
    order = rng.permutation(len(instances))
    ix, iy = crate["inner_half"]
    cp = st["crate_pose"]
    yaw = math.radians(cp["yaw_deg"])
    c, s = math.cos(yaw), math.sin(yaw)
    spawns = []
    for i in order:
        r = parts[instances[i]]["radius"]
        mx, my = max(ix - r - F["margin"], 0.005), max(iy - r - F["margin"], 0.005)
        lx, ly = float(rng.uniform(-mx, mx)), float(rng.uniform(-my, my))
        spawns.append({"part": int(i), "xy": [cp["x"] + c * lx - s * ly, cp["y"] + s * lx + c * ly],
                       "quat": _rand_quat(rng)})
    S = cfg["pile"]["stack"]
    return {"mode": "stack", "spawns": spawns, "floor_z": 0.0, "clearance": S["clearance"],
            "calm_speed": S["calm_speed"], "calm_max_ticks": S["calm_max_ticks"],
            "settle_max_ticks": S["settle_max_ticks"], "rest_speed": F["rest_speed"], "rest_ticks": F["rest_ticks"]}


def _frozen_fill(frozen: dict, cfg: dict) -> dict:
    R = cfg["rain"]
    spawns = [{"part": i, "pos": [float(v) for v in r["pose"][:3]], "quat": [float(v) for v in r["pose"][3:]]}
              for i, r in enumerate(frozen["parts"]) if not r["frozen"]]
    return {"mode": "place", "spawns": spawns, "settle_max_ticks": R["settle_max_ticks"], "rest_speed": R["rest_speed"],
            "rest_ticks": R["rest_ticks"], "frozen": sum(bool(r["frozen"]) for r in frozen["parts"]),
            "hidden_target": int(frozen.get("hidden", -1)), "rain": frozen.get("rain")}


def _rain(m, st, crate, part_rows, instances, parts, cfg, seed) -> dict:
    """Rain schedule: the parts in a random order, split into batches; each batch a set of drop points over the crate
    floor whose bounding circles do not overlap, each part in a random orientation. The sim drops a batch once the pile
    is calm and stops when the pile surface reaches `level` of the crate depth. With `bury`, the largest parts stay out of the rain: once it stops,
    the sim drops them as one tight batch over a `top` part near the centre so it ends under one layer (a `hidden`
    target), checked with classify and retried from a snapshot."""
    R = cfg["rain"]
    rng = np.random.default_rng([int(seed), 0x7A17])
    order = rng.permutation(len(instances))
    bury = None
    if st.get("bury"):
        B = R["bury"]
        k = min(int(B["parts"]), max(1, len(order) // 4))
        c0 = max(range(len(instances)), key=lambda i: (float(parts[instances[i]]["radius"]), -i))
        big = [c0] + [int(i) for i in order if i != c0][:k - 1]   # the largest part, then the next ones of the rain
        order = np.array([i for i in order if i not in set(big)], dtype=int)
        brng = np.random.default_rng([int(seed), 0xB0B])
        rad = [float(parts[instances[i]]["radius"]) for i in big]
        reach = 0.5 * rad[0]
        Rw = _flat(m, part_rows[c0], brng)[0]   # the first part lies flat over its target
        q0 = np.zeros(4)
        mujoco.mju_mat2Quat(q0, Rw.reshape(-1))
        bury = {"parts": [int(i) for i in big], "quats": [[float(v) for v in q0]] + [_rand_quat(brng) for _ in big[1:]], "ring": float(B["ring"]), "size": float(B["size"]),
                "wall": reach + float(B["wall"]), "tries": int(B["tries"]), "salt": int(brng.integers(1 << 30))}
    ix, iy = crate["inner_half"]
    cp = st["crate_pose"]
    yaw = math.radians(cp["yaw_deg"])
    c, s = math.cos(yaw), math.sin(yaw)
    mg, gap, bmax, tries = float(R["margin"]), float(R["gap"]), int(R["batch_max"]), int(R["tries"])
    spawns, batch, bi = [], [], 0
    for i in order:
        r = float(parts[instances[i]]["radius"])
        mx, my = max(ix - r - mg, 0.005), max(iy - r - mg, 0.005)
        pos = None
        if len(batch) < bmax:
            for _ in range(tries):
                lx, ly = float(rng.uniform(-mx, mx)), float(rng.uniform(-my, my))
                if all(math.hypot(lx - x, ly - y) >= r + q + gap for x, y, q in batch):
                    pos = lx, ly
                    break
        if pos is None:
            bi, batch = bi + 1, []
            pos = float(rng.uniform(-mx, mx)), float(rng.uniform(-my, my))
        lx, ly = pos
        batch.append((lx, ly, r))
        spawns.append({"part": int(i), "xy": [cp["x"] + c * lx - s * ly, cp["y"] + s * lx + c * ly],
                       "quat": _rand_quat(rng), "batch": bi})
    keys = ("clearance", "drop", "level_cell", "level_q", "batch_min_ticks", "calm_speed", "calm_max_ticks",
            "settle_max_ticks", "rest_speed", "rest_ticks")
    out = {"mode": "rain", "spawns": spawns, "floor_z": 0.0, "level": float(st["level"]), **{k: R[k] for k in keys}}
    if bury:
        out["bury"] = bury
    return out


def _flat(m, p, rng) -> tuple[np.ndarray, float, np.ndarray, float, list]:
    V = []
    for g in p["hull_geoms"]:
        mid = int(m.geom_dataid[g])
        a, n = int(m.mesh_vertadr[mid]), int(m.mesh_vertnum[mid])
        pts = m.mesh_vert[a:a + n].astype(float)
        ga = int(m.mesh_graphadr[mid])
        if ga >= 0:
            nv = int(m.mesh_graph[ga])
            pts = pts[m.mesh_graph[ga + 2 + nv:ga + 2 + 2 * nv]]
        R = np.zeros(9)
        mujoco.mju_quat2Mat(R, m.geom_quat[g])
        V.append(pts @ R.reshape(3, 3).T + m.geom_pos[g])
    A = np.concatenate(V)
    k = int(np.argmin(A.max(0) - A.min(0)))
    up = 1.0 if rng.random() < 0.5 else -1.0
    e = np.eye(3)
    zb = up * e[k]
    xb = e[(k + 1) % 3]
    Q = np.stack([xb, np.cross(zb, xb), zb])
    psi = float(rng.uniform(0.0, 2 * math.pi))
    Rz = np.array([[math.cos(psi), -math.sin(psi), 0.0], [math.sin(psi), math.cos(psi), 0.0], [0.0, 0.0, 1.0]])
    Rw = Rz @ Q
    W = A @ Rw.T
    lo, hi = W[:, :2].min(0), W[:, :2].max(0)
    c = (lo + hi) / 2
    r = float(np.linalg.norm(W[:, :2] - c, axis=1).max())
    return Rw, float(-W[:, 2].min()), c, r, [v @ Rw.T for v in V]


class _Shape:

    def __init__(self, pieces: list, h: float):
        from scipy.spatial import ConvexHull
        self.planes = []
        for P in pieces:
            try:
                self.planes.append(ConvexHull(P).equations)
            except Exception:
                self.planes.append(ConvexHull(P, qhull_options="QJ").equations)
        A = np.concatenate(pieces)
        lo, hi = A[:, :2].min(0), A[:, :2].max(0)
        X, Y = np.meshgrid(np.arange(lo[0], hi[0] + h, h), np.arange(lo[1], hi[1] + h, h))
        G = np.concatenate([np.stack([X.ravel(), Y.ravel()], 1), A[:, :2]])
        t, b = self.surfaces(G)
        ok = np.isfinite(t)
        self.grid, self.top, self.bot = G[ok], t[ok], b[ok]

    def surfaces(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        top, bot = np.full(len(X), -np.inf), np.full(len(X), np.inf)
        for E in self.planes:
            s, nz = X @ E[:, :2].T + E[:, 3], E[:, 2]
            up, dn = nz > 1e-9, nz < -1e-9
            zt = (-s[:, up] / nz[up]).min(1)
            zb = (-s[:, dn] / nz[dn]).max(1)
            ok = zb <= zt + 1e-7
            side = ~(up | dn)
            if side.any():
                ok &= (s[:, side] <= 1e-7).all(1)
            top = np.where(ok, np.maximum(top, zt), top)
            bot = np.where(ok, np.minimum(bot, zb), bot)
        return top, bot


def _rest_z(a: _Shape, oa, b: _Shape, ob, zb: float) -> tuple[np.ndarray, np.ndarray]:
    tb, _ = b.surfaces(a.grid + (oa - ob))
    _, ba = a.surfaces(b.grid + (ob - oa))
    need = np.concatenate([tb - a.bot, b.top - ba]) + zb
    pts = np.concatenate([a.grid, b.grid + (ob - oa)])
    ok = np.isfinite(need)
    return pts[ok], need[ok]


def _carried(pts: np.ndarray, com, margin: float) -> bool:
    from scipy.spatial import ConvexHull
    if len(pts) < 3 or np.ptp(pts[:, 0]) < 2 * margin or np.ptp(pts[:, 1]) < 2 * margin:
        return False
    try:
        E = ConvexHull(pts).equations
    except Exception:
        return False
    return bool((E[:, :2] @ np.asarray(com) + E[:, 2] <= -margin).all())


def _place(m, st, crate, part_rows, cfg, seed, robot: dict | None = None) -> dict:
    F = cfg["fill"]
    rng = np.random.default_rng([int(seed), 0x91ACE])
    flat = [_flat(m, p, rng) for p in part_rows]
    n = len(part_rows)
    (ix, iy), sp, mg = crate["inner_half"], float(F["spacing"]), float(F["margin"])
    gap, ov, layers = float(F["lift_gap"]), float(F.get("overlap", 0.0)), int(F.get("max_layers", 1))
    by_size = sorted(range(n), key=lambda i: (-flat[i][3], i))
    k = int(math.floor(float(F.get("overlap_share", 0.0)) * n + 1e-9)) if layers > 1 and ov > 0 else 0
    mass = [float(m.body_mass[p["body"]]) for p in part_rows]
    thick = [float(np.ptp(np.concatenate(f[4])[:, 2])) for f in flat]
    com = [(flat[i][0] @ np.asarray(m.body_ipos[p["body"]], float))[:2] for i, p in enumerate(part_rows)]
    tol, cm = float(F.get("rest_tol", 0.0015)), float(F.get("rest_com_margin", 0.004))
    sturdy = [mass[i] >= float(F.get("lean_min_mass", 0.0)) and thick[i] >= float(F.get("lean_min_thick", 0.0))
              for i in range(n)]
    lean = set()
    cand = [i for i in by_size[1:] if sturdy[i]]
    k = min(k, len(cand))
    if k > 0:
        lrng = np.random.default_rng([int(seed), 0x91ACE, 0])
        lean = {cand[int(j)] for j in lrng.choice(len(cand), size=k, replace=False)}
    cp = st["crate_pose"]
    yaw = math.radians(cp["yaw_deg"])
    c, s = math.cos(yaw), math.sin(yaw)

    def origin(lx, ly, i):
        off = flat[i][2]
        return np.array([cp["x"] + c * lx - s * ly - off[0], cp["y"] + s * lx + c * ly - off[1]])

    shapes = {}

    def shape(i):
        if i not in shapes:
            shapes[i] = _Shape(flat[i][4], float(F.get("stack_grid", 0.002)))
        return shapes[i]

    def on_floor(i, placed, prng):
        r = flat[i][3]
        mx, my = ix - r - mg, iy - r - mg
        if mx < 0 or my < 0:
            return None
        for _ in range(int(F["tries_per_part"])):
            lx, ly = float(prng.uniform(-mx, mx)), float(prng.uniform(-my, my))
            if all(math.hypot(lx - q["x"], ly - q["y"]) >= r + q["r"] + sp for q in placed):
                return {"x": lx, "y": ly, "r": r, "i": i, "z": flat[i][1] + gap, "layer": 1, "on": []}
        return None

    def leaning(i, placed, prng):
        r = flat[i][3]
        mx, my = ix - r - mg, iy - r - mg
        low = [q for q in placed if q["layer"] < layers and sturdy[q["i"]] and q["r"] >= 0.75 * r
               and mass[q["i"]] >= 0.5 * mass[i]]
        if mx < 0 or my < 0 or not low:
            return None
        for _ in range(int(F["tries_per_part"])):
            q = low[int(prng.integers(len(low)))]
            th = float(prng.uniform(0.0, 2 * math.pi))
            dd = float(prng.uniform(1.0 - ov, 1.0)) * (r + q["r"])
            lx, ly = q["x"] + dd * math.cos(th), q["y"] + dd * math.sin(th)
            if abs(lx) > mx or abs(ly) > my:
                continue
            ok, near = True, []
            for p in placed:
                dp = math.hypot(lx - p["x"], ly - p["y"])
                if dp < (r + p["r"] + sp if p["layer"] >= layers else (1.0 - ov) * (r + p["r"])):
                    ok = False
                    break
                if dp < r + p["r"] + sp:
                    near.append(p)
            if not ok:
                continue
            oi, pts, need, on = origin(lx, ly, i), [], [], []
            cw = oi + com[i]
            if not any(math.hypot(*(cw - origin(p["x"], p["y"], p["i"]) - flat[p["i"]][2])) < p["r"] for p in near):
                continue
            for p in near:
                xy, z = _rest_z(shape(i), oi, shape(p["i"]), origin(p["x"], p["y"], p["i"]), p["z"])
                if len(z):
                    pts.append(xy)
                    need.append(z)
                    on.append(p)
            if not on:
                continue
            pts, need = np.concatenate(pts), np.concatenate(need)
            z = float(need.max())
            if z < flat[i][1] + 0.003:
                continue
            if not _carried(pts[need >= z - tol], com[i], cm):
                continue
            return {"x": lx, "y": ly, "r": r, "i": i, "z": z + gap,
                    "layer": 1 + max(p["layer"] for p in on), "on": [p["i"] for p in on]}
        return None

    def layout(leaners: set, demoted: list) -> list:
        floor = [i for i in by_size if i not in leaners and i not in demoted]
        for attempt in range(int(F["restarts"])):
            placed = []
            for i in floor + [i for i in by_size if i in leaners] + list(demoted):
                prng = np.random.default_rng([int(seed), 0x91ACE, attempt + 1, i])
                p = (leaning(i, placed, prng) if i in leaners else None) or on_floor(i, placed, prng)
                if p is None:
                    break
                placed.append(p)
            else:
                return placed
        area = sum(math.pi * (f[3] + sp / 2) ** 2 for f in flat)
        raise SceneError(capacity_error(area, n, 4 * ix * iy, cfg, crate))

    def posed(placed):
        poses = []
        for p in placed:
            q = np.zeros(4)
            mujoco.mju_mat2Quat(q, flat[p["i"]][0].reshape(-1))
            poses.append([*origin(p["x"], p["y"], p["i"]), p["z"], *q])
        _unpenetrate(m, part_rows, placed, poses, gap)
        return poses

    demoted: list = []
    for _ in range(int(F.get("check_rounds", 0)) + 1):
        placed = layout(lean, demoted)
        poses = posed(placed)
        top = {k for k, p in enumerate(placed) if p["layer"] > 1}
        if not top or robot is None:
            break
        moving = _moving(m, part_rows, placed, poses, robot, F)
        bad = {placed[k]["i"] for k in top if k in moving or any(placed[j]["i"] in placed[k]["on"] for j in moving)}
        if not bad:
            break
        lean = lean - bad
        demoted += sorted(bad, key=lambda i: (-flat[i][3], i))
    else:
        demoted += sorted(lean, key=lambda i: (-flat[i][3], i))
        lean = set()
        placed = layout(lean, demoted)
        poses = posed(placed)
    spawns = [{"part": int(part_rows[p["i"]]["i"]), "pos": [float(v) for v in pose[:3]],
               "quat": [float(v) for v in pose[3:]], "layer": int(p["layer"])} for p, pose in zip(placed, poses)]
    used = sum(math.pi * (f[3] + sp / 2) ** 2 for f in flat) / (4 * ix * iy)
    return {"mode": "place", "spawns": spawns, "settle_max_ticks": F["settle_max_ticks"],
            "rest_speed": F["rest_speed"], "rest_ticks": F["rest_ticks"], "floor_used": round(used, 4),
            "leaning": sum(p["layer"] > 1 for p in placed)}


def _moving(m, part_rows, placed, poses, robot, F) -> set:
    d = mujoco.MjData(m)
    gc = m.body_gravcomp.copy()
    try:
        for p, pose in zip(placed, poses):
            r = part_rows[p["i"]]
            d.qpos[r["qpos"]:r["qpos"] + 7] = pose
            m.body_gravcomp[r["body"]] = 0.0
        d.qpos[robot["qpos"]] = robot["q0"]
        d.ctrl[robot["act"]] = robot["q0"]
        d.ctrl[robot["grip"]] = 0.0
        dofs = np.array([part_rows[p["i"]]["dof"] for p in placed])[:, None] + np.arange(3)
        bodies = np.array([part_rows[p["i"]]["body"] for p in placed])
        mujoco.mj_forward(m, d)
        flung = np.zeros(len(placed), bool)
        rest = 0
        for t in range(int(F["settle_max_ticks"])):
            mujoco.mj_step(m, d, robot["nsub"])
            v = np.linalg.norm(d.qvel[dofs], axis=1)
            if t >= 3:
                flung |= v > 1.0
            rest = rest + 1 if v.max() < float(F["rest_speed"]) else 0
            if rest >= int(F["rest_ticks"]):
                break
        x0, vmax = d.xpos[bodies].copy(), np.zeros(len(placed))
        for _ in range(int(F.get("check_ticks", 40))):
            mujoco.mj_step(m, d, robot["nsub"])
            vmax = np.maximum(vmax, np.linalg.norm(d.qvel[dofs], axis=1))
        drift = np.linalg.norm(d.xpos[bodies] - x0, axis=1)
    finally:
        m.body_gravcomp[:] = gc
    bad = flung | (vmax > float(F.get("check_speed", 0.002))) | (drift > float(F.get("check_drift", 0.0005)))
    return {int(k) for k in np.flatnonzero(bad)}


def _unpenetrate(m, part_rows, placed, poses, gap, rounds: int = 8) -> None:
    d = mujoco.MjData(m)
    body = {int(part_rows[p["i"]]["body"]): k for k, p in enumerate(placed)}
    for _ in range(rounds):
        for p, pose in zip(placed, poses):
            a = int(part_rows[p["i"]]["qpos"])
            d.qpos[a:a + 7] = pose
        mujoco.mj_fwdPosition(m, d)
        lift = {}
        con = d.contact
        for g1, g2, dist in zip(con.geom1, con.geom2, con.dist):
            b1, b2 = int(m.geom_bodyid[g1]), int(m.geom_bodyid[g2])
            if b1 in body and b2 in body and dist < 0:
                k = max(body[b1], body[b2])
                lift[k] = max(lift.get(k, 0.0), -float(dist) + gap)
        if not lift:
            return
        for k, dz in lift.items():
            poses[k][2] += dz


def _camera(st, crate, cfg) -> dict:
    K = cfg["camera"]
    cp = st["crate_pose"]
    yaw = math.radians(cp["yaw_deg"])
    pos = [cp["x"], cp["y"], crate["rim_z"] - crate["floor_z"] + K["above_rim"]]
    right = [math.cos(yaw), math.sin(yaw), 0.0]
    fwd = [0.0, 0.0, -1.0]
    down = [math.sin(yaw), -math.cos(yaw), 0.0]
    f = (K["height"] / 2) / math.tan(math.radians(K["fovy_deg"]) / 2)
    return {"pos": pos, "R_cam_to_world": [[right[i], down[i], fwd[i]] for i in range(3)],
            "width": K["width"], "height": K["height"], "fx": f, "fy": f, "cx": (K["width"] - 1) / 2,
            "cy": (K["height"] - 1) / 2, "depth_unit": K["depth_unit"], "max_depth": K["max_depth"]}
