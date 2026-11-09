from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import numpy as np
import trimesh
import yaml

HERE = Path(__file__).resolve().parent
MATERIALS = yaml.safe_load((HERE / "materials.yaml").read_text())["materials"]

ROBOT_FACES = 20000
GRIPPER_FACES = 1500
TCP_Z = 0.1439
GRIP_FORCE = 3.0
PAD_FRICTION = 1.0
PAD_PRIORITY = 3
PAD_CONDIM = 4
RAY_FACES = 1500
HULLS_MAX = int(os.environ.get("VECTOR_HULLS_MAX", 2))
HULL_THRESHOLD = float(os.environ.get("VECTOR_HULL_THRESHOLD", 0.08))


def _decimate(m: trimesh.Trimesh, faces: int) -> trimesh.Trimesh:
    if len(m.faces) <= faces:
        return m
    import fast_simplification
    v, f = fast_simplification.simplify(np.asarray(m.vertices, np.float32), np.asarray(m.faces, np.int32),
                                        target_reduction=1.0 - faces / len(m.faces))
    return trimesh.Trimesh(v, f, process=True)


def _stl(m: trimesh.Trimesh, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(m.export(file_type="stl"))


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _one_mesh(scene) -> trimesh.Trimesh:
    if isinstance(scene, trimesh.Trimesh):
        return scene
    return trimesh.util.concatenate([g for g in scene.dump()])


def build_robot(robot_src: Path, out: Path) -> dict:
    import mujoco
    ur = mujoco.MjSpec.from_file(str(robot_src / "universal_robots_ur10e" / "ur10e.xml"))
    gr = mujoco.MjSpec.from_file(str(robot_src / "robotiq_2f85" / "2f85.xml"))
    src_dirs = {id(ur): robot_src / "universal_robots_ur10e" / "assets", id(gr): robot_src / "robotiq_2f85" / "assets"}
    where = {}
    for spec, pre in ((ur, ""), (gr, "g_")):
        for m in spec.meshes:
            where[pre + (m.name or Path(m.file).stem)] = src_dirs[id(spec)] / m.file
            m.file = str(src_dirs[id(spec)] / m.file)
        spec.meshdir = ""
    for k in list(ur.keys):
        ur.delete(k)
    for k in list(gr.keys):
        gr.delete(k)
    act = gr.actuator("fingers_actuator")
    act.forcerange = [-5.0 * GRIP_FORCE, 5.0 * GRIP_FORCE]
    for side in ("left", "right"):
        for k in (1, 2):
            g = gr.geom(f"{side}_pad{k}")
            g.priority, g.condim = PAD_PRIORITY, PAD_CONDIM
            g.friction = [PAD_FRICTION, 0.005, 0.0001]
    for side in ("left", "right"):
        gr.add_equality(type=mujoco.mjtEq.mjEQ_JOINT, name1=f"{side}_spring_link_joint",
                        name2=f"{side}_driver_joint", solref=[0.005, 1], solimp=[0.95, 0.99, 0.001, 0.5, 2])
    gr.body("base").add_site(name="tcp", pos=[0.0, 0.0, TCP_Z], quat=[0.70710678, 0.0, 0.0, 0.70710678], group=5)
    site = ur.site("attachment_site")
    ur.attach(gr, site=site, prefix="g_")
    (out / "robot").mkdir(parents=True, exist_ok=True)
    for m in ur.meshes:
        name = m.name or Path(m.file).stem
        src = where[name]
        tm = trimesh.load(str(src), force="mesh")
        faces = GRIPPER_FACES if src.parent.parent.name == "robotiq_2f85" else ROBOT_FACES
        tm = _decimate(tm, faces)
        rel = f"robot/{name}.stl"
        _stl(tm, out / rel)
        m.file = str(out.resolve() / rel)
    ur.modelname = "ur10e_2f85"
    xml = ur.to_xml().replace(str(out.resolve()) + "/", "")
    (out / "robot" / "robot.xml").write_text(xml)
    ur2 = mujoco.MjSpec.from_string(xml)
    ur2.meshdir = str(out.resolve()) + "/"
    model = ur2.compile()
    return {"xml": "robot/robot.xml", "nq": int(model.nq), "nu": int(model.nu),
            "arm_joints": ["shoulder_pan_joint", "shoulder_lift_joint", "elbow_joint", "wrist_1_joint",
                           "wrist_2_joint", "wrist_3_joint"],
            "gripper_actuator": "g_fingers_actuator", "tcp_site": "g_tcp", "gripper_base": "g_base",
            "driver_joint": "g_right_driver_joint",
            "pads": {"left": ["g_left_pad", "g_left_silicone_pad"], "right": ["g_right_pad", "g_right_silicone_pad"]}}


def build_part(p: dict, src: Path, out: Path, visual_src: Path) -> dict:
    import coacd
    scene = trimesh.load(str(src / "parts" / f"{p['id']}.glb"))
    if isinstance(scene, trimesh.Trimesh):
        scene = trimesh.Scene(scene)
    mesh = _one_mesh(scene)
    c = mesh.bounds.mean(0)
    scene.apply_translation(-c)
    mesh = _one_mesh(scene)
    d = out / "parts" / p["id"]
    d.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(visual_src / "parts" / f"{p['id']}.glb", d / "visual.glb")
    phys = trimesh.Trimesh(mesh.vertices, mesh.faces, process=True)
    phys.merge_vertices()
    _stl(_decimate(phys, RAY_FACES), d / "ray.stl")
    cm = coacd.Mesh(np.asarray(phys.vertices, float), np.asarray(phys.faces, int))
    parts = coacd.run_coacd(cm, threshold=HULL_THRESHOLD, max_convex_hull=HULLS_MAX, preprocess_mode="auto",
                            resolution=2000, mcts_iterations=100, seed=0)
    hulls = []
    for k, (v, f) in enumerate(parts):
        h = trimesh.Trimesh(v, f, process=True).convex_hull
        _stl(h, d / f"hull_{k}.stl")
        hulls.append(h)
    mat = MATERIALS[p["material"]]
    rho = float(mat["density"]) * 1000.0
    body = phys if phys.is_watertight and phys.volume > 0 else trimesh.util.concatenate(hulls).convex_hull
    vol = float(body.volume)
    body.density = rho
    mass = rho * vol
    I = np.asarray(body.moment_inertia, float)
    w, V = np.linalg.eigh(I)
    if np.linalg.det(V) < 0:
        V[:, 0] *= -1
    q = trimesh.transformations.quaternion_from_matrix(np.vstack([np.c_[V, [0, 0, 0]], [0, 0, 0, 1]]))
    return {"id": p["id"], "label": p["label"], "title": p["title"], "material": p["material"],
            "mass": round(mass, 6), "com": np.round(body.center_mass, 6).tolist(),
            "inertia": np.maximum(np.round(w, 10), 1e-9).tolist(), "inertia_quat": np.round(q, 6).tolist(),
            "friction": float(mat["friction"]), "extents": np.round(mesh.extents, 5).tolist(),
            "radius": round(float(np.linalg.norm(mesh.vertices, axis=1).max()), 5),
            "hulls": len(hulls), "visual": f"parts/{p['id']}/visual.glb", "visual_offset": np.round(-c, 6).tolist(),
            "ray": f"parts/{p['id']}/ray.stl",
            "hull_files": [f"parts/{p['id']}/hull_{k}.stl" for k in range(len(hulls))]}


def build_crate(c: dict, visual_src: Path, out: Path) -> dict:
    f = Path(c["file"]).name
    (out / "crates").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(visual_src / "crates" / f, out / "crates" / f)
    return {k: c[k] for k in ("id", "label", "color", "scale", "outer_half", "inner_half", "floor_z", "rim_z")} | {
        "visual": f"crates/{f}"}


def build(src: Path, robot_src: Path, out: Path, only: list[str] | None = None, visual_src: Path | None = None) -> dict:
    src, out = Path(src), Path(out)
    visual_src = Path(visual_src or src)
    lib_src = json.loads((src / "library.src.json").read_text())
    out.mkdir(parents=True, exist_ok=True)
    old = json.loads((out / "library.json").read_text()) if (out / "library.json").exists() else {}
    robot = build_robot(Path(robot_src), out) if not only or "robot" in only else old["robot"]
    old_parts = {p["id"]: p for p in old.get("parts", [])}
    parts = []
    for p in lib_src["parts"]:
        if only and p["id"] not in only and p["id"] in old_parts:
            parts.append(old_parts[p["id"]])
            continue
        print("part", p["id"], flush=True)
        parts.append(build_part(p, src, out, visual_src))
    crates = [build_crate(c, visual_src, out) for c in lib_src["crates"]]
    lib = {"format": "vector-cell/1", "robot": robot, "parts": parts, "crates": crates}
    (out / "library.json").write_text(json.dumps(lib, indent=1))
    h, entries = write_manifest(out)
    print(f"bundle {h}: {len(entries)} files, {sum(e['bytes'] for e in entries) / 1e6:.1f} MB")
    from .units import build as build_source
    build_source("demo", out, force=True, procs=1)
    return lib


CORE = ("library.json", "robot", "crates", "parts")


def write_manifest(out: Path) -> tuple[str, list]:
    files = []
    for top in CORE:
        p = out / top
        if p.is_file():
            files.append(top)
        elif p.is_dir():
            files += [str(q.relative_to(out)) for q in p.rglob("*") if q.is_file()]
    entries = [{"path": f, "sha256": _sha(out / f), "bytes": (out / f).stat().st_size} for f in sorted(files)]
    h = hashlib.sha256("".join(e["path"] + e["sha256"] for e in entries).encode()).hexdigest()[:16]
    (out / "manifest.json").write_text(json.dumps({"bundle": h, "files": entries}, indent=1))
    return h, entries
