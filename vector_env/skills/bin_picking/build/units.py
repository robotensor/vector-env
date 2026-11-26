from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path

from . import source
from ..library import write_index

USD_PYTHON = os.environ.get("VECTOR_USD_PYTHON", "")
_job: dict = {}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _flat(files: dict) -> list[str]:
    out = []
    for v in files.values():
        out += v if isinstance(v, list) else [v]
    return [f for f in out if f]


def _done(out: Path, version: int) -> bool:
    aj = out / "asset.json"
    if not aj.is_file():
        return False
    try:
        return json.loads(aj.read_text()).get("version") == version
    except Exception:
        return False


def _work(unit: str):
    mod, src, bundle = _job["mod"], _job["src"], _job["bundle"]
    rel = f"assets/{mod.PREFIX}/{unit}"
    t0 = time.perf_counter()
    try:
        rec = mod.build_unit(unit, src, bundle / rel, rel)
        files = _flat(rec["files"])
        rec["sha256"] = {f: _sha(bundle / f) for f in files}
        rec["bytes"] = {f: (bundle / f).stat().st_size for f in files}
        sha = rec["sha256"]
        for r in rec["rows"]:
            r["sha256"] = {"ray": sha[r["files"]["ray"]], "hulls": [sha[h] for h in r["files"]["hulls"]]}
        rec["build_s"] = round(time.perf_counter() - t0, 3)
        tmp = bundle / rel / "asset.json.tmp"
        tmp.write_text(json.dumps(rec, indent=1))
        tmp.replace(bundle / rel / "asset.json")
        return unit, True, rec["build_s"], sum(rec["bytes"].values()), None
    except Exception:
        return unit, False, time.perf_counter() - t0, 0, traceback.format_exc(limit=4)


def _ensure_python(mod) -> None:
    if not mod.NEEDS_USD:
        return
    try:
        import pxr  # noqa: F401
        return
    except ImportError:
        pass
    if not USD_PYTHON or not Path(USD_PYTHON).is_file():
        raise SystemExit(f"source {mod.NAME} reads USD: pxr is missing here; set VECTOR_USD_PYTHON to a python that has "
                         f"it (is {USD_PYTHON!r})")
    root = str(Path(__file__).resolve().parents[4])
    env = {**os.environ, "PYTHONPATH": root + os.pathsep + os.environ.get("PYTHONPATH", "")}
    print(f"pxr missing: re-running under {USD_PYTHON}", flush=True)
    os.execve(USD_PYTHON, [USD_PYTHON, "-m", "vector_env.skills.bin_picking.build", *sys.argv[1:]], env)


def build(name: str, bundle: Path, src: str | None = None, procs: int = 8, limit: int | None = None,
          units_file: str | None = None, force: bool = False, index_every: int = 2000) -> dict:
    import multiprocessing as mp
    mod = source(name)
    _ensure_python(mod)
    bundle = Path(bundle).resolve()
    src = src or mod.DEFAULT_SRC or (str(bundle) if getattr(mod, "SRC_IS_BUNDLE", False) else None)
    if not src:
        raise SystemExit(f"source {name}: give its input with --src (or its environment variable, see "
                         f"vector_env/skills/bin_picking/build/{name}.py)")
    t0 = time.time()
    units = mod.units(src)
    if units_file:
        want = set(Path(units_file).read_text().split())
        units = [u for u in units if u in want]
    if limit:
        units = sorted(units, key=lambda u: hashlib.sha256(u.encode()).hexdigest())[:limit]
    root = bundle / "assets" / mod.PREFIX
    todo = [u for u in units if force or not _done(root / u, mod.VERSION)]
    print(f"{name}: {len(units)} units, {len(units) - len(todo)} already built, {len(todo)} to build "
          f"({procs} procs, listed in {time.time() - t0:.1f} s)", flush=True)
    _job.update(mod=mod, src=src, bundle=bundle)
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[k] = "1"
    errs = root / "_errors.jsonl"
    root.mkdir(parents=True, exist_ok=True)
    n_ok = n_err = 0
    build_s = size = 0.0
    t1 = time.time()
    if todo:
        ctx = mp.get_context("fork")
        with ctx.Pool(procs, maxtasksperchild=500) as workers, open(errs, "a") as ef:
            for k, (unit, ok, dt, nbytes, err) in enumerate(workers.imap_unordered(_work, todo, chunksize=2), 1):
                if ok:
                    n_ok += 1
                    build_s += dt
                    size += nbytes
                else:
                    n_err += 1
                    ef.write(json.dumps({"unit": unit, "error": err, "at": time.time()}) + "\n")
                    ef.flush()
                if k % 100 == 0 or k == len(todo):
                    el = time.time() - t1
                    print(f"  {k}/{len(todo)}  ok {n_ok} err {n_err}  {k / el:.1f} units/s  "
                          f"{build_s / max(n_ok, 1):.2f} s/unit/proc  {size / max(n_ok, 1) / 1e3:.0f} kB/unit  "
                          f"eta {(len(todo) - k) / (k / el) / 60:.1f} min", flush=True)
                if index_every and k % index_every == 0:
                    write_index(bundle)
    facets = write_index(bundle)
    out = {"source": name, "units": len(units), "built": n_ok, "errors": n_err, "wall_s": round(time.time() - t0, 1),
           "s_per_unit_proc": round(build_s / max(n_ok, 1), 3), "bytes_per_unit": int(size / max(n_ok, 1)),
           "assets_total": facets["count"]}
    print(json.dumps(out), flush=True)
    return out
