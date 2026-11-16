from __future__ import annotations

import queue
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import assets
from .node import NodeProc, sim
from .settings import env_config


def docker_defaults() -> dict:
    return dict(env_config()["harness"]["docker"])


def docker_argv(image: str, name: str, docker: dict | None = None, bundle: Path | None = None) -> tuple[list, list]:
    D = {**docker_defaults(), **(docker or {})}
    b = str(D.get("binary") or "docker")
    mem = str(D["memory"])
    nofile = int(D.get("nofile") or 1024)
    argv = [b, "run", "--rm", "-i", "--pull", "never", "--network", "none", "--name", name,
            "--cpus", str(D["cpus"]), "--memory", mem, "--memory-swap", mem, "--pids-limit", str(int(D.get("pids") or 256)),
            "--read-only", "--tmpfs", f"/tmp:rw,nosuid,nodev,size={int(D.get('tmpfs_mb') or 1024)}m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--ipc", "private",
            "--ulimit", f"nofile={nofile}:{nofile}", "--ulimit", "core=0"]
    if D.get("user"):
        argv += ["--user", str(D["user"]), "-e", "HOME=/tmp"]
    if D.get("userns"):
        argv += ["--userns", str(D["userns"])]
    if D.get("bundle", True) and bundle is not None:
        argv += ["-v", f"{bundle}:/bundle:ro", "-e", "VECTOR_SIM_BUNDLE=/bundle"]
    argv += [str(x) for x in D.get("args") or []]
    argv.append(image)
    return argv, [b, "kill", name]


def pull(image: str, docker: dict | None = None, timeout: float = 1800) -> None:
    b = str({**docker_defaults(), **(docker or {})}.get("binary") or "docker")
    p = subprocess.run([b, "pull", image], capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"docker pull {image} failed: {p.stderr.decode(errors='replace')[-400:]}")


def _expert_spec(expert: dict, docker: dict | None, bundle: Path) -> dict:
    if expert.get("command"):
        cmd = expert["command"]
        argv = shlex.split(cmd) if isinstance(cmd, str) else [str(x) for x in cmd]
        return {"argv": argv, "kill": None, "cwd": str(expert.get("cwd") or Path.cwd()),
                "env": {"VECTOR_SIM_BUNDLE": str(bundle), **(expert.get("env") or {})}}
    if expert.get("image"):
        name = f"vector-env-{uuid.uuid4().hex[:12]}"
        argv, kill = docker_argv(expert["image"], name, docker, bundle)
        return {"argv": argv, "kill": kill, "cwd": str(Path.cwd()), "env": {}}
    raise ValueError("expert needs an image or a command")


def _wire(scene: dict) -> dict:
    return {k: scene.get(k) for k in ("scene_id", "xml", "meta", "checkpoint", "state_hash", "target")}


def _fail(scene: dict, outcome: str, error: str, wall: float) -> dict:
    return {"scene_id": scene.get("scene_id"), "outcome": outcome, "success": False, "t": 0.0, "ctrl": "", "n": 0,
            "wall_s": round(wall, 3), "error": error}


def run(expert: dict, scenes: list[dict], *, time_limit_s: float, wall_s: float, workers: int = 1,
        docker: dict | None = None, io: dict | None = None, on_result=None) -> list[dict]:
    bundle = assets.ensure()
    H = env_config()["harness"]
    grace = float(H.get("grace_s", 2.0))
    io = {**(H.get("io") or {}), **(io or {})}
    out: list[dict | None] = [None] * len(scenes)
    todo: queue.Queue = queue.Queue()
    for i in range(len(scenes)):
        todo.put(i)
    lock = threading.Lock()

    def worker() -> None:
        proc = NodeProc("harness.mjs", bundle)
        try:
            while True:
                try:
                    i = todo.get_nowait()
                except queue.Empty:
                    return
                sc = scenes[i]
                t0 = time.monotonic()
                spec = _expert_spec(expert, docker, bundle)
                job = {"id": i, "scene": _wire(sc), "expert": spec, "time_limit_s": float(time_limit_s),
                       "wall_s": float(wall_s), "grace_s": grace, "io": io}
                try:
                    r = proc.call(job, timeout=float(wall_s) + grace + 60)
                    r.pop("id", None)
                except Exception as e:
                    if spec.get("kill"):
                        subprocess.run(spec["kill"], capture_output=True, timeout=30)
                    r = _fail(sc, "harness_error", str(e)[:500], time.monotonic() - t0)
                if r.get("scene_id") is None:
                    r["scene_id"] = sc.get("scene_id")
                out[i] = r
                if on_result:
                    with lock:
                        on_result(i, r)
        finally:
            proc.close()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, min(int(workers), len(scenes))))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [r if r is not None else _fail(scenes[i], "harness_error", "not run", 0.0) for i, r in enumerate(out)]


def replay(scene: dict, ctrl_b64: str, n: int, *, time_limit_s: float) -> dict:
    r = sim(assets.ensure()).call({"op": "replay", "scene": _wire(scene), "ctrl": ctrl_b64, "n": int(n),
                                   "time_limit_s": float(time_limit_s)})
    r.pop("id", None)
    return r


def summary(results: list[dict]) -> dict:
    n = len(results)
    ok = [r for r in results if r.get("success")]
    outcomes: dict[str, int] = {}
    for r in results:
        outcomes[r.get("outcome") or "none"] = outcomes.get(r.get("outcome") or "none", 0) + 1
    ex = [r["exchange"] for r in results if (r.get("exchange") or {}).get("n")]
    tot = sum(e["n"] for e in ex)
    return {"scenes": n, "successes": len(ok), "score": round(len(ok) / n, 4) if n else 0.0,
            "mean_time_s": round(sum(r["t"] for r in ok) / len(ok), 3) if ok else None,
            "outcomes": dict(sorted(outcomes.items())),
            "mean_wall_s": round(sum(r.get("wall_s") or 0 for r in results) / n, 3) if n else None,
            "exchanges": tot,
            "exchange_mean_ms": round(sum(e["mean_ms"] * e["n"] for e in ex) / tot, 4) if tot else None}
