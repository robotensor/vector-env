from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np
import trimesh
from scipy.spatial import ConvexHull


def decimate(m: trimesh.Trimesh, faces: int) -> trimesh.Trimesh:
    if len(m.faces) <= faces:
        return m
    import fast_simplification
    v, f = fast_simplification.simplify(np.asarray(m.vertices, np.float32), np.asarray(m.faces, np.int32),
                                        target_reduction=1.0 - faces / len(m.faces))
    out = trimesh.Trimesh(v, f, process=True)
    for _ in range(4):
        if len(out.faces) <= faces:
            break
        v, f = fast_simplification.simplify(np.asarray(out.vertices, np.float32), np.asarray(out.faces, np.int32),
                                            target_reduction=min(0.99, 1.0 - 0.9 * faces / len(out.faces)))
        out = trimesh.Trimesh(v, f, process=True)
    if len(out.faces) > faces:
        out = cluster(m, faces)
    return out


def cluster(m: trimesh.Trimesh, faces: int) -> trimesh.Trimesh:
    v, f = np.asarray(m.vertices, float), np.asarray(m.faces, np.int64)
    ext = float(np.max(m.extents)) or 1.0

    def snap(cell):
        key = np.floor((v - v.min(0)) / cell).astype(np.int64)
        _, inv = np.unique(key, axis=0, return_inverse=True)
        inv = inv.reshape(-1)
        nv = np.zeros((inv.max() + 1, 3))
        np.add.at(nv, inv, v)
        nv /= np.bincount(inv)[:, None]
        nf = inv[f]
        nf = nf[(nf[:, 0] != nf[:, 1]) & (nf[:, 1] != nf[:, 2]) & (nf[:, 0] != nf[:, 2])]
        nf = np.unique(nf, axis=0)
        return nv, nf

    lo, hi, best = ext / 2000, ext / 4, None
    for _ in range(14):
        mid = (lo * hi) ** 0.5
        nv, nf = snap(mid)
        if len(nf) <= faces:
            best, hi = (nv, nf), mid
        else:
            lo = mid
    if best is None:
        return m.convex_hull
    out = trimesh.Trimesh(*best, process=True)
    if len(out.faces) > faces or len(out.faces) < 4:
        return m.convex_hull if len(m.convex_hull.faces) <= faces else out
    return out


def stl_bytes(m: trimesh.Trimesh) -> bytes:
    return m.export(file_type="stl")


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _hull_vol(p: np.ndarray) -> float:
    try:
        return float(ConvexHull(p).volume)
    except Exception:
        return 0.0


def merge_hulls(pieces: list[np.ndarray], k: int = 2) -> list[np.ndarray]:
    pieces = [np.asarray(p, float) for p in pieces if len(p) >= 4]
    if len(pieces) <= k:
        return [p[ConvexHull(p).vertices] for p in pieces]
    groups = [[i] for i in range(len(pieces))]
    pts = [pieces[i] for i in range(len(pieces))]
    vols = [_hull_vol(p) for p in pts]
    cache: dict[tuple, float] = {}

    def joint(a: tuple, b: tuple, pa, pb) -> float:
        key = (a, b)
        if key not in cache:
            cache[key] = _hull_vol(np.vstack([pa, pb]))
        return cache[key]

    while len(groups) > k:
        best = None
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                v = joint(tuple(groups[i]), tuple(groups[j]), pts[i], pts[j])
                c = v - vols[i] - vols[j]
                if best is None or c < best[0]:
                    best = (c, i, j, v)
        _, i, j, v = best
        g, p = groups[i] + groups[j], np.vstack([pts[i], pts[j]])
        try:
            p = p[ConvexHull(p).vertices]
        except Exception:
            pass
        groups = [x for t, x in enumerate(groups) if t not in (i, j)] + [sorted(g)]
        pts = [x for t, x in enumerate(pts) if t not in (i, j)] + [p]
        vols = [x for t, x in enumerate(vols) if t not in (i, j)] + [v]
    if k == 2:
        improved, rounds = True, 0
        while improved and rounds < 3:
            improved, rounds = False, rounds + 1
            for piece in range(len(pieces)):
                src = 0 if piece in groups[0] else 1
                if len(groups[src]) == 1:
                    continue
                a = [i for i in groups[src] if i != piece]
                b = groups[1 - src] + [piece]
                va = _hull_vol(np.vstack([pieces[i] for i in a]))
                vb = _hull_vol(np.vstack([pieces[i] for i in b]))
                if va + vb < vols[0] + vols[1] - 1e-12:
                    groups[src], groups[1 - src] = a, sorted(b)
                    vols[src], vols[1 - src] = va, vb
                    improved = True
        pts = [np.vstack([pieces[i] for i in g]) for g in groups]
    out = []
    for p in pts:
        try:
            out.append(p[ConvexHull(p).vertices])
        except Exception:
            out.append(p)
    return out


def hull_mesh(points: np.ndarray, max_vertices: int = 64) -> trimesh.Trimesh:
    p = np.asarray(points, float)
    p = p[ConvexHull(p).vertices]
    if len(p) > max_vertices:
        c = p.mean(0)
        sel = [int(np.argmax(np.linalg.norm(p - c, axis=1)))]
        d = np.linalg.norm(p - p[sel[0]], axis=1)
        while len(sel) < max_vertices:
            i = int(np.argmax(d))
            sel.append(i)
            d = np.minimum(d, np.linalg.norm(p - p[i], axis=1))
        q = p[sel]
        h = trimesh.Trimesh(q, ConvexHull(q).simplices, process=True)
        h.fix_normals()
        c = q.mean(0)
        n, o = h.face_normals, h.triangles[:, 0, :]
        num = (p - c) @ n.T
        den = np.einsum("ij,ij->i", o - c, n)
        s = float(max(1.0, np.max(num / np.maximum(den, 1e-12))))
        q = c + (q - c) * s
        p = q
    h = trimesh.Trimesh(p, ConvexHull(p).simplices, process=True)
    h.fix_normals()
    return h


def _pad4(b: bytes, fill: bytes = b"\0") -> bytes:
    return b + fill * ((4 - len(b) % 4) % 4)


def smooth_normals(m: trimesh.Trimesh, angle_deg: float = 35.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    try:
        s = trimesh.graph.smooth_shade(m, angle=np.radians(angle_deg))
        return np.asarray(s.vertices), np.asarray(s.vertex_normals), np.asarray(s.faces)
    except Exception:
        return np.asarray(m.vertices), np.asarray(m.vertex_normals), np.asarray(m.faces)


def glb_bytes(v: np.ndarray, n: np.ndarray, f: np.ndarray, color, metallic: float, roughness: float,
              name: str = "part") -> bytes:
    v = np.asarray(v, np.float64)
    lo, hi = v.min(0), v.max(0)
    centre = (lo + hi) / 2
    half = np.maximum((hi - lo) / 2, 1e-9)
    scale = half / 32767.0
    q = np.round((v - centre) / scale).clip(-32767, 32767).astype(np.int16)
    pos = np.zeros((len(v), 4), np.int16)
    pos[:, :3] = q
    nn = np.asarray(n, np.float64)
    nn = nn / np.maximum(np.linalg.norm(nn, axis=1, keepdims=True), 1e-12)
    nq = np.zeros((len(v), 4), np.int8)
    nq[:, :3] = np.round(nn * 127).clip(-127, 127).astype(np.int8)
    big = len(v) > 65535
    idx = np.asarray(f, np.uint32 if big else np.uint16).reshape(-1)
    b_pos, b_nrm, b_idx = pos.tobytes(), nq.tobytes(), _pad4(idx.tobytes())
    blob = b_pos + b_nrm + b_idx
    qmin, qmax = q.min(0).astype(int).tolist(), q.max(0).astype(int).tolist()
    gltf = {
        "asset": {"version": "2.0", "generator": "vector-env"},
        "extensionsUsed": ["KHR_mesh_quantization"], "extensionsRequired": ["KHR_mesh_quantization"],
        "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": name, "translation": [float(x) for x in centre],
                   "scale": [float(x) for x in scale]}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "NORMAL": 1}, "indices": 2, "material": 0}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorFactor": [float(c) for c in color[:3]] + [1.0],
                                                "metallicFactor": float(metallic), "roughnessFactor": float(roughness)}}],
        "buffers": [{"byteLength": len(blob)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(b_pos), "byteStride": 8, "target": 34962},
            {"buffer": 0, "byteOffset": len(b_pos), "byteLength": len(b_nrm), "byteStride": 4, "target": 34962},
            {"buffer": 0, "byteOffset": len(b_pos) + len(b_nrm), "byteLength": int(idx.nbytes), "target": 34963}],
        "accessors": [
            {"bufferView": 0, "componentType": 5122, "count": len(v), "type": "VEC3", "min": qmin, "max": qmax},
            {"bufferView": 1, "componentType": 5120, "normalized": True, "count": len(v), "type": "VEC3"},
            {"bufferView": 2, "componentType": 5125 if big else 5123, "count": int(idx.size), "type": "SCALAR"}],
    }
    js = _pad4(json.dumps(gltf, separators=(",", ":")).encode(), b" ")
    total = 12 + 8 + len(js) + 8 + len(blob)
    return (struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<II", len(js), 0x4E4F534A) + js
            + struct.pack("<II", len(blob), 0x004E4942) + blob)
