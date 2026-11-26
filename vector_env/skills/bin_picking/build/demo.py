from __future__ import annotations

import json
from pathlib import Path

NAME = "demo"
PREFIX = "demo"
NEEDS_USD = False
DEFAULT_SRC = None
SRC_IS_BUNDLE = True
VERSION = 1

CLASSES = {"gear": "turned", "knob": "moulded", "pulley": "turned", "bushing": "turned", "screw": "turned",
           "bearing": "bulk", "grommet": "moulded", "flange": "turned", "caster": "bulk", "junction": "moulded",
           "collar": "turned", "bracket": "sheet"}


MATERIALS = {"aluminum": "aluminium", "stainless_steel": "stainless"}


def _parts(bundle: Path) -> dict:
    return {p["id"]: p for p in json.loads((Path(bundle) / "library.json").read_text())["parts"]}


def units(src) -> list[str]:
    return sorted(_parts(src))


def build_unit(pid: str, src, out: Path, rel: str) -> dict:
    p = _parts(src)[pid]
    out.mkdir(parents=True, exist_ok=True)
    files = {"ray": p["ray"], "hulls": list(p["hull_files"]), "visual": p["visual"]}
    size_mm = max(p["extents"]) * 1000
    row = {"id": pid, "source": NAME, "unit": pid, "stem": pid, "variant": None, "label": p["label"],
           "title": p.get("title"), "class": CLASSES.get(pid, "bulk"),
           "material": MATERIALS.get(p["material"], p["material"]), "surface": None,
           "mass": p["mass"], "com": p["com"], "inertia": p["inertia"], "inertia_quat": p["inertia_quat"],
           "mu_static": p["friction"], "mu_dynamic": p["friction"], "restitution": None,
           "extents_m": p["extents"], "size_mm": round(size_mm, 1), "radius": p["radius"], "hulls": p["hulls"],
           "files": files, "thumb": None, "visual_offset": p["visual_offset"], "look": None}
    return {"unit": pid, "source": NAME, "version": VERSION, "files": files, "geom": {}, "rows": [row]}
