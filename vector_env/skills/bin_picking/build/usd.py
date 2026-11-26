from __future__ import annotations

import json
import os
import shutil
from functools import lru_cache
from pathlib import Path

import numpy as np
import trimesh

from . import meshes

NAME = "library"
PREFIX = "lib"
NEEDS_USD = True
DEFAULT_SRC = os.environ.get("VECTOR_ASSETS_SRC")
VERSION = 1

RAY_FACES = 1500
VISUAL_FACES = 3000
VISUAL_BYTES = 60_000
HULLS_MAX = 2
HULL_VERTS = 64
SINGLE_HULL_SLACK = 1.05

MATERIAL_NAMES = {"carbon_steel": "carbon steel", "stainless": "stainless steel", "aluminium": "aluminium",
                  "cast_iron": "cast iron", "brass": "brass", "zinc": "zinc die-cast", "nylon": "nylon", "pom": "POM",
                  "abs": "ABS"}
SURFACE_NAMES = {"dry": "", "oiled": "oiled", "zinc_plated": "zinc-plated", "black_oxide": "black oxide",
                 "anodised": "anodised"}
KEEP = ("variant", "tier", "class", "material", "surface", "density", "mass_kg", "com_m", "diag_inertia_kgm2",
        "principal_axes_wxyz", "mu_static", "mu_dynamic", "restitution", "roughness", "metallic", "base_color",
        "extents_mm", "fits", "hull_count", "t50_mm", "dup_group")


@lru_cache(None)
def _manifest(src: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    with open(Path(src) / "manifest.jsonl") as f:
        for line in f:
            r = json.loads(line)
            row = {k: r.get(k) for k in KEEP}
            row["grasp_2f85"] = bool(((r.get("grasp") or {}).get("robotiq_2f85") or {}).get("pass"))
            out.setdefault(r["stem"], []).append(row)
    for rows in out.values():
        rows.sort(key=lambda r: r["variant"])
    return out


def units(src) -> list[str]:
    return sorted(_manifest(str(src)))


def label(cls: str, size_mm: float, material: str, surface: str | None) -> str:
    s = f"{cls} part {size_mm:.0f} mm, {MATERIAL_NAMES.get(material, material.replace('_', ' '))}"
    sf = SURFACE_NAMES.get(surface or "dry", (surface or "").replace("_", " "))
    return f"{s}, {sf}" if sf else s


def read_usd(path: Path) -> tuple[np.ndarray, np.ndarray, list[np.ndarray]]:
    from pxr import Usd, UsdGeom
    st = Usd.Stage.Open(str(path))
    vis, pieces = None, []
    for p in st.Traverse():
        if p.GetTypeName() != "Mesh":
            continue
        m = UsdGeom.Mesh(p)
        xf = np.asarray(UsdGeom.Xformable(p).ComputeLocalToWorldTransform(Usd.TimeCode.Default()), float)
        v = np.asarray(m.GetPointsAttr().Get(), float)
        if not np.allclose(xf, np.eye(4)):
            v = v @ xf[:3, :3] + xf[3, :3]
        if p.GetName() == "visual":
            counts = np.asarray(m.GetFaceVertexCountsAttr().Get())
            idx = np.asarray(m.GetFaceVertexIndicesAttr().Get(), np.int64)
            if not np.all(counts == 3):
                raise ValueError("visual mesh is not triangulated")
            vis = (v, idx.reshape(-1, 3))
        else:
            pieces.append(v)
    if vis is None:
        raise ValueError("no /Object/visual mesh")
    return vis[0], vis[1], pieces


def build_unit(stem: str, src, out: Path, rel: str) -> dict:
    src = Path(src)
    rows = _manifest(str(src))[stem]
    v, f, pieces = read_usd(src / "assets" / stem / "base.usdc")
    mesh = trimesh.Trimesh(v, f, process=True)
    out.mkdir(parents=True, exist_ok=True)

    if not pieces:
        pieces = [np.asarray(mesh.convex_hull.vertices)]
    merged = meshes.merge_hulls(pieces, HULLS_MAX)
    hulls = [meshes.hull_mesh(p, HULL_VERTS) for p in merged]
    if len(hulls) == 2:
        one = meshes.hull_mesh(np.vstack(pieces), HULL_VERTS)
        if one.volume <= SINGLE_HULL_SLACK * (hulls[0].volume + hulls[1].volume):
            hulls = [one]
    for k, h in enumerate(hulls):
        meshes.write(out / f"hull_{k}.stl", meshes.stl_bytes(h))
    for k in range(len(hulls), HULLS_MAX):
        (out / f"hull_{k}.stl").unlink(missing_ok=True)

    ray = meshes.decimate(mesh, RAY_FACES)
    meshes.write(out / "ray.stl", meshes.stl_bytes(ray))
    r0 = rows[0]
    faces = VISUAL_FACES
    while True:
        vis = meshes.decimate(mesh, faces)
        vv, nn, ff = meshes.smooth_normals(vis)
        glb = meshes.glb_bytes(vv, nn, ff, r0["base_color"], r0["metallic"], r0["roughness"], name=stem)
        if len(glb) <= VISUAL_BYTES or faces < 500:
            break
        faces = int(faces * 0.75)
    meshes.write(out / "visual.glb", glb)
    thumb = None
    if (src / "assets" / stem / "thumb.jpg").is_file():
        shutil.copyfile(src / "assets" / stem / "thumb.jpg", out / "thumb.jpg")
        thumb = f"{rel}/thumb.jpg"

    ext = mesh.extents
    hv = np.vstack([h.vertices for h in hulls])
    radius = float(max(np.linalg.norm(mesh.vertices, axis=1).max(), np.linalg.norm(hv, axis=1).max()))
    size_mm = float(ext.max() * 1000)
    files = {"ray": f"{rel}/ray.stl", "hulls": [f"{rel}/hull_{k}.stl" for k in range(len(hulls))],
             "visual": f"{rel}/visual.glb"}
    hull_ratio = float(sum(h.volume for h in hulls) / mesh.volume) if mesh.volume > 0 else None
    out_rows = []
    for r in rows:
        out_rows.append({
            "id": f"{PREFIX}-{stem}-{r['variant']}", "source": NAME, "unit": stem, "stem": stem, "variant": r["variant"],
            "label": label(r["class"], size_mm, r["material"], r["surface"]), "class": r["class"],
            "material": r["material"], "surface": r["surface"], "tier": r["tier"],
            "mass": round(float(r["mass_kg"]), 6), "com": [round(float(x), 7) for x in r["com_m"]],
            "inertia": [max(float(x), 1e-10) for x in r["diag_inertia_kgm2"]],
            "inertia_quat": [round(float(x), 8) for x in r["principal_axes_wxyz"]],
            "mu_static": round(float(r["mu_static"]), 4), "mu_dynamic": round(float(r["mu_dynamic"]), 4),
            "restitution": round(float(r["restitution"]), 4), "density": round(float(r["density"]), 1),
            "extents_m": [round(float(x), 5) for x in ext], "size_mm": round(size_mm, 1), "radius": round(radius, 5),
            "hulls": len(hulls), "files": files, "thumb": thumb, "visual_offset": [0.0, 0.0, 0.0],
            "look": {"color": [float(c) for c in r["base_color"]], "metallic": float(r["metallic"]),
                     "roughness": round(float(r["roughness"]), 3)},
            "fits": r["fits"], "grasp_2f85": r["grasp_2f85"],
        })
    return {"unit": stem, "source": NAME, "version": VERSION, "files": files | ({"thumb": thumb} if thumb else {}),
            "geom": {"faces_src": int(len(f)), "faces_ray": int(len(ray.faces)), "faces_visual": int(len(ff)),
                     "pieces_src": len(pieces), "hull_mesh_volume": round(hull_ratio, 3) if hull_ratio else None},
            "rows": out_rows}
