from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _bundle(a) -> Path:
    if a.bundle:
        return Path(a.bundle)
    from ..assets import bundle_dir
    return bundle_dir()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vector_env.build")
    ap.add_argument("--bundle", help="bundle directory (default: the bundle vector-env uses)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cell", help="build the robot, crates and the demo parts into the bundle")
    c.add_argument("--src", required=True)
    c.add_argument("--robot-src", required=True)
    c.add_argument("--visual-src")
    c.add_argument("--only", nargs="*")
    b = sub.add_parser("build", help="build a source's assets (resumable) and rewrite the index")
    b.add_argument("--source", required=True)
    b.add_argument("--src", help="the source's input (default: the source's DEFAULT_SRC)")
    b.add_argument("--procs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    b.add_argument("--limit", type=int, help="only this many units (a fixed spread-out sample)")
    b.add_argument("--units", help="file with the units to build (whitespace separated)")
    b.add_argument("--force", action="store_true", help="rebuild units that are already built")
    b.add_argument("--index-every", type=int, default=2000)
    sub.add_parser("index", help="rewrite library/index.{jsonl,sqlite} and facets.json from the built assets")
    q = sub.add_parser("query", help="search the index")
    q.add_argument("--q")
    q.add_argument("--source")
    q.add_argument("--class", dest="cls")
    q.add_argument("--material")
    q.add_argument("--size", nargs=2, type=float, metavar=("LO_MM", "HI_MM"))
    q.add_argument("--sort", default="id")
    q.add_argument("--page", type=int, default=1)
    q.add_argument("--per-page", type=int, default=20)
    g = sub.add_parser("get", help="one asset's full row")
    g.add_argument("id")
    a = ap.parse_args(argv)
    bundle = _bundle(a)
    if a.cmd == "cell":
        from .cell import build as build_cell
        build_cell(Path(a.src), Path(a.robot_src), bundle, a.only, a.visual_src)
    elif a.cmd == "build":
        from .units import build
        build(a.source, bundle, a.src, a.procs, a.limit, a.units, a.force, a.index_every)
    elif a.cmd == "index":
        from ..library import write_index
        write_index(bundle)
    elif a.cmd == "query":
        from ..library import search
        total, rows = search(bundle, {"q": a.q, "source": a.source, "class": a.cls, "material": a.material,
                                      "size_mm": a.size}, a.sort, a.page, a.per_page)
        print(f"{total} matching")
        for r in rows:
            print(f"{r['id']:70s} {r['label']:45s} {r['mass']:.3f} kg")
    elif a.cmd == "get":
        from ..library import get
        json.dump(get(bundle, a.id), sys.stdout, indent=1)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
