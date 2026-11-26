from __future__ import annotations

import argparse
import json
import queue
import shlex
import sys
import threading
import time
from pathlib import Path

from vector_env import __version__


def _err(*x) -> None:
    print(*x, file=sys.stderr, flush=True)


class EvalError(RuntimeError):
    pass


def draw_scenes(n: int, seed: int, draw: dict | None = None, log=_err, *, workers: int = 1, cache=None,
                stats: dict | None = None) -> tuple[list[dict], list[int]]:
    from . import scenes
    from .compose import SceneError
    from .node import close_thread_sims
    got: list[tuple | None] = [None] * n
    scs, skipped, fatal, crash = [], [], [], []
    stop = threading.Event()
    lock = threading.Lock()
    nxt = [0]

    def one(i: int) -> tuple:
        if cache is not None:
            hit = cache.get(seed, i)
            if hit is not None:
                return hit[0], hit[1], True
        sc, err = scenes.draw_index(seed, i, draw)
        if cache is not None:
            cache.put(seed, i, sc, err)
        return sc, err, False

    def emit(i: int) -> None:
        sc, err, hit = got[i]
        tag = " (cached)" if hit else ""
        if stats is not None and hit:
            stats["cached"] = stats.get("cached", 0) + 1
        if sc is None:
            if not scs and not isinstance(err, SceneError):
                fatal.append(EvalError(f"the scene generator does not work here ({type(err).__name__}: {err}): "
                                       "check vector-env, node and the asset bundle"))
                stop.set()
                return
            skipped.append(i)
            log(f"scene {i + 1}/{n}: skipped (none of its {scenes.SEED_TRIES} draws places a valid scene, "
                f"as in the round: {err}){tag}")
            return
        scs.append(sc)
        k = int(sc.get("seed", 0)) - scenes.seed_for(seed, i)
        log(f"scene {i + 1}/{n}: {sc['scene_id']} ({len(sc['meta']['parts'])} parts)"
            + (f", seed + {k} as in the round" if 0 < k < scenes.SEED_TRIES else "") + tag)

    def done(i: int, x: tuple) -> None:
        with lock:
            got[i] = x
            while nxt[0] < n and got[nxt[0]] is not None and not fatal:
                emit(nxt[0])
                nxt[0] += 1

    todo: queue.Queue = queue.Queue()
    for i in range(n):
        todo.put(i)

    def worker(close: bool) -> None:
        try:
            while not stop.is_set():
                try:
                    i = todo.get_nowait()
                except queue.Empty:
                    return
                done(i, one(i))
        except BaseException as e:
            crash.append(e)
            stop.set()
        finally:
            if close:
                close_thread_sims()

    k = max(1, min(int(workers), n))
    if k == 1:
        worker(False)
    else:
        threads = [threading.Thread(target=worker, args=(True,), daemon=True) for _ in range(k)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    if crash:
        raise crash[0]
    if fatal:
        raise fatal[0]
    if n and not scs:
        raise EvalError(f"none of the {n} indexes places a valid scene with this draw")
    return scs, skipped


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


def scene_cache(root, draw: dict | None = None):
    from . import assets, scenes
    if root is False:
        return None
    d = scenes.scene_cache_root(root)
    if d is None:
        return None
    try:
        env = scenes.env_info(assets.ensure())
    except assets.AssetsError:
        return None
    return scenes.SceneCache(d, env, draw)


def write_traces(trace_dir: str | Path, scs: list[dict], results: list[dict], *, time_limit_s: float, workers: int = 1,
                 log=_err) -> list[Path]:
    from . import harness
    from .node import close_thread_sims
    folder = Path(trace_dir)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise EvalError(f"cannot write traces to {folder}: {e.strerror or e}") from None
    todo: queue.Queue = queue.Queue()
    for i, r in enumerate(results):
        if not r.get("infra") and isinstance(r.get("ctrl"), str) and isinstance(r.get("n"), int):
            todo.put(i)
    out: dict[int, tuple] = {}
    lock = threading.Lock()

    def worker(close: bool) -> None:
        try:
            while True:
                try:
                    i = todo.get_nowait()
                except queue.Empty:
                    return
                sc, r = scs[i], results[i]
                p = harness.scene_file(folder, i, sc, "json")
                try:
                    tr = harness.trace(sc, r["ctrl"], r["n"], time_limit_s=time_limit_s, result=r)
                    p.write_text(json.dumps(tr, separators=(",", ":")))
                except Exception as e:
                    with lock:
                        out[i] = (None, f"{type(e).__name__}: {e}")
                    continue
                with lock:
                    out[i] = (p, tr)
        finally:
            if close:
                close_thread_sims()

    k = max(1, min(int(workers), todo.qsize()))
    if k == 1:
        worker(False)
    else:
        threads = [threading.Thread(target=worker, args=(True,), daemon=True) for _ in range(k)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    written = []
    for i in sorted(out):
        p, tr = out[i]
        sid = scs[i].get("scene_id")
        if p is None:
            log(f"trace {sid}: not written ({tr})")
            continue
        written.append(p)
        if not results[i].get("success"):
            log(f"why {sid}: " + " | ".join(tr["reasons"]))
    log(f"traces: {len(written)} of {len(results)} scenes in {folder}")
    return written


def evaluate(expert: dict, n: int, seed: int, *, workers: int = 1, time_limit_s: float | None = None,
             wall_s: float | None = None, docker: dict | None = None, draw: dict | None = None, log=_err,
             pull: bool = False, stderr_dir: str | None = None, scene_cache_dir=None,
             trace_dir: str | None = None) -> dict:
    from . import harness, rules, scenes
    from .settings import env_config
    task = rules.task(time_limit_s if time_limit_s is not None else rules.defaults()["time_limit_s"])
    wall = float(wall_s if wall_s is not None else env_config()["harness"]["wall_s"])
    t0 = time.time()
    cache = scene_cache(scene_cache_dir, draw)
    stats = {"cached": 0}
    scs, skipped = draw_scenes(n, seed, draw, log, workers=workers, cache=cache, stats=stats)
    gen_s = time.time() - t0

    def show(i, r):
        log(f"{r['scene_id']}: {r['outcome']} t={r['t']} s rows={r['n']} wall={r['wall_s']} s"
            + (f"  ({r['error']})" if r.get("error") else ""))

    t1 = time.time()
    res = harness.run(expert, scs, time_limit_s=task["time_limit_s"], wall_s=wall, workers=workers, docker=docker,
                      on_result=show, pull=pull, stderr_dir=stderr_dir)
    out = harness.summary(res)
    out.update(seed=seed, skipped=skipped, seed_tries=scenes.SEED_TRIES, time_limit_s=task["time_limit_s"], wall_s=wall, workers=workers,
               generate_s=round(gen_s, 1), scenes_cached=stats["cached"], scene_cache=str(cache.dir) if cache else None,
               run_s=round(time.time() - t1, 1))
    if trace_dir:
        write_traces(trace_dir, scs, res, time_limit_s=task["time_limit_s"], workers=workers, log=log)
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


def infra_problem(summary: dict, results: list[dict]) -> str | None:
    n, bad = summary.get("scenes") or 0, summary.get("infra") or 0
    if not n or bad < n:
        return None
    r = next((x for x in results if x.get("infra")), results[0])
    msg = f"no scene ran here ({r.get('outcome')}: {r.get('error')}); this is not a score"
    if r.get("outcome") == "docker_unavailable":
        msg += ". --image needs a running docker daemon; without one, test with --command"
    return msg


def report(r: dict, results_path: str | None, prog: str = "vector-env") -> int:
    if results_path:
        with open(results_path, "w") as f:
            for x in r["results"]:
                f.write(json.dumps(x, separators=(",", ":")) + "\n")
    s = r["summary"]
    why = infra_problem(s, r["results"])
    if why:
        _err(f"{prog}: {why}")
        return 2
    print(json.dumps(s))
    if s.get("infra"):
        kinds = sorted({x.get("outcome") for x in r["results"] if x.get("infra")})
        _err(f"{prog}: warning: {s['infra']} of {s['scenes']} scenes did not run here ({', '.join(kinds)}); "
             "the score counts them as failures")
    return 0


def cmd_run(a) -> int:
    ex = _expert(a)
    r = evaluate(ex, a.scenes, a.seed, workers=a.workers, time_limit_s=a.time_limit, wall_s=a.wall, docker=_docker(a),
                 draw=json.loads(a.draw) if a.draw else None, pull=a.pull, stderr_dir=a.stderr,
                 scene_cache_dir=False if a.no_scene_cache else a.scene_cache, trace_dir=a.trace)
    return report(r, a.results)


def _read_json(path: str, what: str):
    try:
        text = Path(path).read_text()
    except OSError as e:
        raise EvalError(f"cannot read the {what} {path}: {e.strerror or e}") from None
    try:
        return json.loads(text)
    except ValueError:
        pass
    rows = []
    for k, line in enumerate(text.splitlines()):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                raise EvalError(f"the {what} {path} is not JSON (line {k + 1})") from None
    return rows


def pick_result(sc: dict, res, path: str) -> dict:
    if isinstance(res, dict):
        return res
    rows = [x for x in res if isinstance(x, dict)] if isinstance(res, list) else []
    mine = [x for x in rows if x.get("scene_id") == sc.get("scene_id")]
    if mine:
        return mine[0]
    have = ", ".join(str(x.get("scene_id")) for x in rows[:5]) + (" ..." if len(rows) > 5 else "")
    raise EvalError(f"{path} has no result for scene {sc.get('scene_id')} (it has {have or 'none'})")


def cmd_replay(a) -> int:
    from . import harness
    sc = _read_json(a.scene, "scene")
    if not isinstance(sc, dict) or not sc.get("xml"):
        raise EvalError(f"{a.scene} is not a scene (write one with vector-env scene --seed N > scene.json)")
    res = pick_result(sc, _read_json(a.result, "result"), a.result)
    if not isinstance(res.get("ctrl"), str) or not isinstance(res.get("n"), int):
        raise EvalError(f"{a.result}: the result has no ctrl/n (use a line of run --results)")
    if res.get("scene_id") is None and res.get("state_hash") is None:
        _err("vector-env: warning: the result names no scene_id; it cannot be checked against the scene")
    if not a.trace:
        print(json.dumps(harness.replay(sc, res["ctrl"], res["n"], time_limit_s=a.time_limit, result=res)))
        return 0
    tr = harness.trace(sc, res["ctrl"], res["n"], time_limit_s=a.time_limit, result=res)
    try:
        Path(a.trace).write_text(json.dumps(tr, separators=(",", ":")))
    except OSError as e:
        raise EvalError(f"cannot write {a.trace}: {e.strerror or e}") from None
    for line in tr["reasons"]:
        _err(f"why: {line}")
    print(json.dumps({k: tr[k] for k in ("outcome", "success", "t", "tick", "hash", "detail", "rows", "held")}))
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


def main(argv: list[str] | None = None, prog: str = "vector-env") -> int:
    ap = argparse.ArgumentParser(prog=prog)
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
    r.add_argument("--stderr", metavar="DIR", help="write the expert's stderr of every scene to DIR/<index, 4 digits>-<scene_id>.log")
    r.add_argument("--trace", metavar="DIR", help="after the run, replay every scene and write DIR/<index, 4 digits>-<scene_id>.json "
                   "(per tick: row, joints, grip, tcp pose, contacts; why it ended)")
    r.add_argument("--scene-cache", metavar="DIR", help="keep generated scenes here (default $VECTOR_ENV_SCENE_CACHE, "
                   "else ~/.cache/vector-env/scenes)")
    r.add_argument("--no-scene-cache", action="store_true", help="generate every scene afresh and keep none")
    p = sub.add_parser("replay", help="re-simulate a stored result's controls on its scene")
    p.add_argument("--scene", required=True)
    p.add_argument("--result", required=True, help="a result (JSON), or a run --results file: the line of this scene")
    p.add_argument("--time-limit", type=float, default=15.0)
    p.add_argument("--trace", metavar="FILE", help="write the per-tick trace (JSON) here and print why the scene ended")
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
    from .compose import SceneError
    from .harness import HarnessError
    from .node import SimError
    try:
        return {"scene": cmd_scene, "run": cmd_run, "replay": cmd_replay, "assets": cmd_assets}[x.cmd](x)
    except SceneError as e:
        hint = f" (seed {x.seed}; run and the round try the next seeds of such an index)" if x.cmd == "scene" else ""
        _err(f"vector-env: no valid scene: {e}{hint}")
        return 2
    except (AssetsError, EvalError, HarnessError, SimError) as e:
        _err(f"vector-env: {e}")
        return 2
    except ValueError as e:
        _err(f"vector-env: {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
