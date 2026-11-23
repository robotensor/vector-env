from __future__ import annotations

import json
import platform
import queue
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import assets
from .node import NodeProc, sim
from .rules import INFRA_OUTCOMES
from .settings import env_config

GB = 1 << 30


class HarnessError(RuntimeError):
    outcome = "harness_error"


class DockerUnavailable(HarnessError):
    outcome = "docker_unavailable"


class ImageMissing(HarnessError):
    outcome = "image_missing"


class PullFailed(HarnessError):
    outcome = "pull_failed"


class ImageTooLarge(HarnessError):
    outcome = "image_too_large"


class ReplayMismatch(HarnessError):
    outcome = "replay_mismatch"


def docker_defaults() -> dict:
    return dict(env_config()["harness"]["docker"])


def docker_argv(image: str, name: str, docker: dict | None = None, bundle: Path | None = None,
                cidfile: Path | None = None) -> tuple[list, list]:
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
    if cidfile is not None:
        argv += ["--cidfile", str(cidfile)]
    argv += [str(x) for x in D.get("args") or []]
    argv.append(image)
    return argv, [b, "kill", name]


def _docker(docker: dict | None) -> dict:
    return {**docker_defaults(), **(docker or {})}


def _bin(docker: dict | None) -> str:
    return str(_docker(docker).get("binary") or "docker")


def _limit(docker: dict | None, key: str) -> int | None:
    v = _docker(docker).get(key)
    return int(float(v) * GB) if v not in (None, "", 0) and float(v) > 0 else None


def _tail(b: bytes) -> str:
    return b.decode(errors="replace").strip()[-400:]


def _docker_cmd(docker: dict | None, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    b = _bin(docker)
    try:
        return subprocess.run([b, *args], capture_output=True, timeout=timeout)
    except FileNotFoundError as e:
        raise DockerUnavailable(f"no docker binary {b!r}: {e}") from e
    except PermissionError as e:
        raise DockerUnavailable(f"cannot run the docker binary {b!r}: {e}") from e


def docker_check(docker: dict | None = None) -> None:
    try:
        p = _docker_cmd(docker, "version", "--format", "{{.Server.Version}}", timeout=30)
    except subprocess.TimeoutExpired as e:
        raise DockerUnavailable("the docker daemon did not answer within 30 s") from e
    if p.returncode != 0 or not p.stdout.strip():
        raise DockerUnavailable(f"the docker daemon is not reachable: {_tail(p.stderr)}")


def image_size(image: str, docker: dict | None = None) -> int | None:
    try:
        p = _docker_cmd(docker, "image", "inspect", "--format", "{{.Size}}", image, timeout=60)
    except subprocess.TimeoutExpired as e:
        raise DockerUnavailable("docker image inspect did not answer within 60 s") from e
    if p.returncode != 0:
        return None
    try:
        return int(p.stdout.strip() or 0)
    except ValueError:
        return 0


def _arch() -> str:
    m = platform.machine().lower()
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(m, m)


def manifest_size(image: str, docker: dict | None = None) -> int:
    try:
        p = _docker_cmd(docker, "manifest", "inspect", "--verbose", image, timeout=120)
    except subprocess.TimeoutExpired as e:
        raise PullFailed(f"reading the manifest of {image} took more than 120 s") from e
    if p.returncode != 0:
        raise PullFailed(f"cannot read the manifest of {image}: {_tail(p.stderr)}")
    try:
        doc = json.loads(p.stdout)
    except ValueError as e:
        raise PullFailed(f"the manifest of {image} is not JSON") from e
    sized = []
    for e in doc if isinstance(doc, list) else [doc]:
        if not isinstance(e, dict):
            continue
        m = e.get("SchemaV2Manifest") or e.get("OCIManifest") or {}
        layers = m.get("layers") if isinstance(m, dict) else None
        if not isinstance(layers, list) or not layers:
            continue
        try:
            total = sum(int(x.get("size") or 0) for x in layers) + int((m.get("config") or {}).get("size") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        plat = (e.get("Descriptor") or {}).get("platform") or {}
        sized.append((plat.get("os") == "linux" and plat.get("architecture") == _arch(), total))
    if not sized:
        raise PullFailed(f"the manifest of {image} lists no layer sizes")
    mine = [t for ok, t in sized if ok]
    return max(mine or [t for _, t in sized])


def _too_large(image: str, size: int, limit: int, what: str) -> ImageTooLarge:
    return ImageTooLarge(f"{image} is {size / GB:.2f} GB {what}, above the limit of {limit / GB:g} GB")


def pull(image: str, docker: dict | None = None, timeout: float | None = None) -> None:
    D = _docker(docker)
    cap, unpacked = _limit(docker, "max_image_gb"), _limit(docker, "max_unpacked_gb")
    if cap is not None:
        size = manifest_size(image, docker)
        if size > cap:
            raise _too_large(image, size, cap, "compressed")
    t = float(timeout if timeout is not None else D.get("pull_timeout_s") or 1800)
    try:
        p = _docker_cmd(docker, "pull", image, timeout=t)
    except subprocess.TimeoutExpired as e:
        raise PullFailed(f"docker pull {image} took more than {t:g} s") from e
    if p.returncode != 0:
        raise PullFailed(f"docker pull {image} failed: {_tail(p.stderr)}")
    have = image_size(image, docker)
    if have is None:
        raise PullFailed(f"docker pull {image} finished but the image is not there")
    if unpacked is not None and have > unpacked:
        remove_image(image, docker)
        raise _too_large(image, have, unpacked, "unpacked")


def remove_image(image: str, docker: dict | None = None) -> bool:
    try:
        return _docker_cmd(docker, "image", "rm", "--force", image, timeout=300).returncode == 0
    except (HarnessError, subprocess.TimeoutExpired):
        return False


def ensure_image(image: str, docker: dict | None = None, pull_missing: bool = True) -> None:
    docker_check(docker)
    have = image_size(image, docker)
    if have is None:
        if not pull_missing:
            raise ImageMissing(f"{image} is not on this machine (pull it first)")
        pull(image, docker)
        return
    unpacked = _limit(docker, "max_unpacked_gb")
    if unpacked is not None and have > unpacked:
        raise _too_large(image, have, unpacked, "unpacked")


def prepare(expert: dict, docker: dict | None = None, pull_missing: bool = False) -> tuple[str, str] | None:
    if expert.get("command") or not expert.get("image"):
        return None
    try:
        ensure_image(str(expert["image"]), docker, pull_missing)
    except HarnessError as e:
        return e.outcome, str(e)[:500]
    return None


def _expert_spec(expert: dict, docker: dict | None, bundle: Path, cids: str | None = None,
                 stderr_file: str | None = None) -> dict:
    if expert.get("command"):
        cmd = expert["command"]
        argv = shlex.split(cmd) if isinstance(cmd, str) else [str(x) for x in cmd]
        spec = {"argv": argv, "kill": None, "cwd": str(expert.get("cwd") or Path.cwd()),
                "env": {"VECTOR_SIM_BUNDLE": str(bundle), **(expert.get("env") or {})}}
    elif expert.get("image"):
        name = f"vector-env-{uuid.uuid4().hex[:12]}"
        cid = Path(cids) / f"{name}.cid" if cids else None
        argv, kill = docker_argv(expert["image"], name, docker, bundle, cid)
        spec = {"argv": argv, "kill": kill, "cwd": str(Path.cwd()), "env": {}, "docker": True,
                "cidfile": str(cid) if cid else None}
    else:
        raise ValueError("expert needs an image or a command")
    if stderr_file:
        spec["stderr_file"] = str(stderr_file)
    return spec


def stderr_path(stderr_dir: str | Path, index: int, scene: dict) -> Path:
    return Path(stderr_dir) / f"{index:04d}-{scene.get('scene_id') or 'scene'}.log"


def _wire(scene: dict) -> dict:
    return {k: scene.get(k) for k in ("scene_id", "xml", "meta", "checkpoint", "state_hash", "target")}


def _fail(scene: dict, outcome: str, error: str, wall: float) -> dict:
    return {"scene_id": scene.get("scene_id"), "outcome": outcome, "success": False, "t": 0.0, "ctrl": "", "n": 0,
            "wall_s": round(wall, 3), "error": error, "infra": outcome in INFRA_OUTCOMES}


def run(expert: dict, scenes: list[dict], *, time_limit_s: float, wall_s: float, workers: int = 1,
        docker: dict | None = None, io: dict | None = None, on_result=None, pull: bool = False,
        stderr_dir: str | Path | None = None) -> list[dict]:
    bad = prepare(expert, docker, pull)
    if bad is not None:
        out = [_fail(sc, bad[0], bad[1], 0.0) for sc in scenes]
        for i, r in enumerate(out):
            if on_result:
                on_result(i, r)
        return out
    bundle = assets.ensure()
    if stderr_dir:
        Path(stderr_dir).mkdir(parents=True, exist_ok=True)
    cids = tempfile.mkdtemp(prefix="vector-env-cid-") if expert.get("image") and not expert.get("command") else None
    try:
        return _run(expert, scenes, bundle, cids, time_limit_s=time_limit_s, wall_s=wall_s, workers=workers,
                    docker=docker, io=io, on_result=on_result, stderr_dir=stderr_dir)
    finally:
        if cids:
            shutil.rmtree(cids, ignore_errors=True)


def _run(expert: dict, scenes: list[dict], bundle: Path, cids: str | None, *, time_limit_s: float, wall_s: float,
         workers: int, docker: dict | None, io: dict | None, on_result,
         stderr_dir: str | Path | None = None) -> list[dict]:
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
                spec = _expert_spec(expert, docker, bundle, cids,
                                    stderr_path(stderr_dir, i, sc) if stderr_dir else None)
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
                r["infra"] = r.get("outcome") in INFRA_OUTCOMES
                if spec.get("cidfile"):
                    Path(spec["cidfile"]).unlink(missing_ok=True)
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


def check_replay(scene: dict, result: dict) -> None:
    for k in ("scene_id", "state_hash"):
        got, want = result.get(k), scene.get(k)
        if got is not None and want is not None and got != want:
            raise ReplayMismatch(f"the result is from scene {result.get('scene_id') or got}, not {scene.get('scene_id')} "
                                 f"({k} differs): replay it on its own scene")


def replay(scene: dict, ctrl_b64: str, n: int, *, time_limit_s: float, result: dict | None = None) -> dict:
    if result is not None:
        check_replay(scene, result)
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
            "infra": sum(1 for r in results if r.get("outcome") in INFRA_OUTCOMES),
            "mean_wall_s": round(sum(r.get("wall_s") or 0 for r in results) / n, 3) if n else None,
            "exchanges": tot,
            "exchange_mean_ms": round(sum(e["mean_ms"] * e["n"] for e in ex) / tot, 4) if tot else None}
