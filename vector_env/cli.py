from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path

from . import __version__


def _err(*x) -> None:
    print(*x, file=sys.stderr, flush=True)


def cmd_scene(a) -> int:
    from . import scenes
    sc = scenes.generate(a.seed, json.loads(a.draw) if a.draw else None)
    if a.summary:
        m = sc["meta"]
        json.dump({"scene_id": sc["scene_id"], "seed": sc["seed"], "target": sc["target"], "state_hash": sc["state_hash"],
                   "crate": m["crate"]["id"], "parts": len(m["parts"]), "counts": m["counts"], "env": sc["env"]}, sys.stdout)
    else:
        json.dump(sc, sys.stdout, separators=(",", ":"))
    print()
    return 0


def evaluate(expert: dict, n: int, seed: int, *, workers: int = 1, time_limit_s: float | None = None,
             wall_s: float | None = None, docker: dict | None = None, draw: dict | None = None, log=_err,
             pull: bool = False) -> dict:
    from . import harness, rules, scenes
    from .settings import env_config
    task = rules.task(time_limit_s if time_limit_s is not None else rules.defaults()["time_limit_s"])
    wall = float(wall_s if wall_s is not None else env_config()["harness"]["wall_s"])
    t0 = time.time()
    scs = []
    for i in range(n):
        scs.append(scenes.generate(scenes.seed_for(seed, i), draw))
        log(f"scene {i + 1}/{n}: {scs[-1]['scene_id']} ({len(scs[-1]['meta']['parts'])} parts)")
    gen_s = time.time() - t0

    def show(i, r):
        log(f"{r['scene_id']}: {r['outcome']} t={r['t']} s rows={r['n']} wall={r['wall_s']} s"
            + (f"  ({r['error']})" if r.get("error") else ""))

    t1 = time.time()
    res = harness.run(expert, scs, time_limit_s=task["time_limit_s"], wall_s=wall, workers=workers, docker=docker,
                      on_result=show, pull=pull)
    out = harness.summary(res)
    out.update(seed=seed, time_limit_s=task["time_limit_s"], wall_s=wall, workers=workers,
               generate_s=round(gen_s, 1), run_s=round(time.time() - t1, 1))
    return {"summary": out, "results": res, "scenes": scs}


def _expert(a) -> dict:
    if a.command:
        return {"command": shlex.split(a.command)}
    if a.image:
        return {"image": a.image}
    raise SystemExit("give --image REF or --command \"...\"")


def _docker(a) -> dict:
    d = {}
    for k in ("cpus", "memory"):
        if getattr(a, k) is not None:
            d[k] = getattr(a, k)
    if a.docker:
        d["binary"] = a.docker
    return d


def cmd_run(a) -> int:
    ex = _expert(a)
    r = evaluate(ex, a.scenes, a.seed, workers=a.workers, time_limit_s=a.time_limit, wall_s=a.wall, docker=_docker(a),
                 draw=json.loads(a.draw) if a.draw else None, pull=a.pull)
    if a.results:
        with open(a.results, "w") as f:
            for x in r["results"]:
                f.write(json.dumps(x, separators=(",", ":")) + "\n")
    print(json.dumps(r["summary"]))
    return 0


def cmd_replay(a) -> int:
    from . import harness
    sc = json.loads(Path(a.scene).read_text())
    res = json.loads(Path(a.result).read_text())
    print(json.dumps(harness.replay(sc, res["ctrl"], res["n"], time_limit_s=a.time_limit)))
    return 0


def cmd_assets(a) -> int:
    from . import assets
    if a.acmd == "pull":
        pin = assets.pinned()
        v = a.version or pin["version"]
        sha = pin["sha256"] if v == pin["version"] else None
        print(str(assets.pull(v, a.source, sha256=sha) if a.force or a.source else assets.ensure(v)))
    elif a.acmd == "dir":
        d = assets.find()
        if d is None:
            raise assets.AssetsError(assets.MISSING)
        print(str(d))
    elif a.acmd == "info":
        d = assets.bundle_dir()
        out = {"dir": str(d), **assets.info(d), "pinned": assets.pinned()}
        if a.verify:
            bad = assets.verify(d)
            out["verified"] = not bad
            out["bad"] = bad[:20]
        print(json.dumps(out, indent=1))
    elif a.acmd == "manifest":
        print(json.dumps(assets.write_manifest(Path(a.bundle) if a.bundle else assets.bundle_dir(), a.version)))
    elif a.acmd == "pack":
        print(json.dumps(assets.pack(Path(a.bundle) if a.bundle else assets.bundle_dir(), Path(a.out), a.version)))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vector-env")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scene", help="generate one scene (JSON on stdout)")
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--draw", help="JSON overrides of the random draw")
    s.add_argument("--summary", action="store_true")
    r = sub.add_parser("run", help="local evaluation: N random scenes from a seed, run the expert, print the score")
    r.add_argument("--image")
    r.add_argument("--command")
    r.add_argument("--scenes", type=int, default=20)
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--workers", type=int, default=1)
    r.add_argument("--time-limit", type=float)
    r.add_argument("--wall", type=float)
    r.add_argument("--cpus", type=float)
    r.add_argument("--memory")
    r.add_argument("--docker", help="the docker binary")
    r.add_argument("--pull", action="store_true", help="docker pull the image first if it is not on this machine (size limits apply)")
    r.add_argument("--draw", help="JSON overrides of the random draw")
    r.add_argument("--results", help="write the per-scene results here (JSON lines)")
    p = sub.add_parser("replay", help="re-simulate a stored result's controls on its scene")
    p.add_argument("--scene", required=True)
    p.add_argument("--result", required=True)
    p.add_argument("--time-limit", type=float, default=15.0)
    a = sub.add_parser("assets", help="the asset bundle")
    asub = a.add_subparsers(dest="acmd", required=True)
    ap_pull = asub.add_parser("pull", help="download and verify the pinned (or given) bundle version")
    ap_pull.add_argument("--version")
    ap_pull.add_argument("--source")
    ap_pull.add_argument("--force", action="store_true")
    asub.add_parser("dir", help="print the bundle folder in use (exit 2 if there is none)")
    ap_info = asub.add_parser("info", help="the bundle in use: version and manifest sha256")
    ap_info.add_argument("--verify", action="store_true", help="check every file's sha256")
    ap_man = asub.add_parser("manifest", help="write the bundle's manifest (assets.json)")
    ap_man.add_argument("--version", required=True)
    ap_man.add_argument("--bundle")
    ap_pack = asub.add_parser("pack", help="write <out>/<version>/{assets.json,bundle.tar} for publishing")
    ap_pack.add_argument("--out", required=True)
    ap_pack.add_argument("--version")
    ap_pack.add_argument("--bundle")
    x = ap.parse_args(argv)
    from .assets import AssetsError
    try:
        return {"scene": cmd_scene, "run": cmd_run, "replay": cmd_replay, "assets": cmd_assets}[x.cmd](x)
    except AssetsError as e:
        _err(f"vector-env: {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
