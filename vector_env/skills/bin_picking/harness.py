from __future__ import annotations

import asyncio
import inspect
import json
import platform
import queue
import shlex
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import assets
from . import policy as P
from .node import NodeProc, SimCrashed, sim
from .rules import HARNESS_OUTCOMES, INFRA_OUTCOMES
from .settings import env_config

GB = 1 << 30
FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3
EMPTY_ACTION_HASH = f"{FNV_OFFSET:016x}"


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


class ChannelClosed(Exception):
    pass


class Reconnected(Exception):
    pass


def action_hash(rows) -> str:
    h = FNV_OFFSET
    for r in rows:
        for b in struct.pack(f"<{len(r)}d", *(float(x) + 0.0 for x in r)):
            h = ((h ^ b) * FNV_PRIME) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


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


def scene_file(folder: str | Path, index: int, scene: dict, ext: str) -> Path:
    return Path(folder) / f"{index:04d}-{scene.get('scene_id') or 'scene'}.{ext}"


def stderr_path(stderr_dir: str | Path, index: int, scene: dict) -> Path:
    return scene_file(stderr_dir, index, scene, "log")


def _wire(scene: dict) -> dict:
    return {k: scene.get(k) for k in ("scene_id", "xml", "meta", "checkpoint", "state_hash", "target")}


def _fail(scene: dict, outcome: str, error: str, wall: float) -> dict:
    return {"scene_id": scene.get("scene_id"), "outcome": outcome, "success": False, "t": 0.0, "ctrl": "", "n": 0,
            "wall_s": round(wall, 3), "error": error, "infra": outcome in INFRA_OUTCOMES, "action_hash": None}


def run(expert: dict, scenes: list[dict], *, time_limit_s: float, wall_s: float, workers: int = 1,
        docker: dict | None = None, io: dict | None = None, on_result=None, pull: bool = False,
        stderr_dir: str | Path | None = None, step_deadline_s: float | None = None,
        max_restarts: int | None = None, first_step_deadline_s: float | None = None,
        restarts_used: list[int] | None = None) -> list[dict]:
    if expert.get("remote") is not None or expert.get("policy") is not None:
        coro = arun(expert, scenes, time_limit_s=time_limit_s, wall_s=wall_s, workers=workers, io=io,
                    on_result=on_result, step_deadline_s=step_deadline_s, max_restarts=max_restarts,
                    first_step_deadline_s=first_step_deadline_s, restarts_used=restarts_used)
        if expert.get("loop") is not None:
            return asyncio.run_coroutine_threadsafe(coro, expert["loop"]).result()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        coro.close()
        raise RuntimeError("harness.run was called inside a running event loop: use await harness.arun(...)")
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


def trace(scene: dict, ctrl_b64: str, n: int, *, time_limit_s: float, result: dict | None = None) -> dict:
    if result is not None:
        check_replay(scene, result)
    r = sim(assets.ensure()).call({"op": "trace", "scene": _wire(scene), "ctrl": ctrl_b64, "n": int(n),
                                   "time_limit_s": float(time_limit_s)})
    r.pop("id", None)
    if result is None:
        return r
    got = result.get("outcome")
    r["result"] = {k: result.get(k) for k in ("outcome", "success", "t", "n", "error", "detail", "wall_s")}
    if got in HARNESS_OUTCOMES:
        r["reasons"].insert(0, f"the harness ended the scene as {got}: {result.get('error')}; the expert's rows end "
                               f"at tick {n}, and the trace holds the last row from there until the rules decide")
    elif got is not None and got != r["outcome"]:
        r["reasons"].insert(0, f"warning: the run gave {got}, this replay {r['outcome']}: the run's simulator or "
                               "scene differs from this one")
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


async def _maybe(x):
    return await x if inspect.isawaitable(x) else x


_CLOSED, _RESTART = object(), object()
CTRL = 7


def act_error(m, max_abs: float | None = None) -> str | None:
    if not isinstance(m, dict):
        return "a message must be a JSON object"
    if m.get("type") == "invalid":
        return str(m.get("error") or "the act was not valid")[:300]
    if m.get("type") != "act":
        return None
    lim = float(max_abs if max_abs is not None else (env_config()["harness"].get("remote") or {}).get("max_abs", 100.0))
    rows = m.get("rows")
    if not isinstance(rows, list):
        return "act.rows must be a list of rows"
    for j, r in enumerate(rows):
        if not isinstance(r, list) or len(r) != CTRL:
            return f"act.rows[{j}] is not a row of {CTRL} numbers"
        for k, x in enumerate(r):
            if isinstance(x, bool) or not isinstance(x, (int, float)):
                return f"act.rows[{j}][{k}] is not a number"
            if x != x or x in (float("inf"), float("-inf")):
                return f"act.rows[{j}][{k}] is not a finite number"
            if abs(x) > lim:
                return f"act.rows[{j}][{k}] = {x:g} is outside -{lim:g}..{lim:g}"
    return None


class _Box:
    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self.flood = False


class _Mux:
    def __init__(self, channel, max_queued: int):
        self.ch = channel
        self.max_queued = max_queued
        self.boxes: dict[str, _Box] = {}
        self.closed: tuple[str, str] | None = None
        self.dropped = 0

    def open(self, key: str) -> _Box:
        b = self.boxes[key] = _Box()
        if self.closed:
            b.q.put_nowait((_CLOSED, 0.0))
        return b

    def drop(self, key: str) -> None:
        self.boxes.pop(key, None)

    def paused(self) -> bool:
        if self.closed:
            return False
        p = getattr(self.ch, "paused", None)
        if p is None:
            return False
        return bool(p() if callable(p) else p)

    async def resumed(self, box: _Box) -> float:
        t = time.monotonic()
        while self.paused() and box.q.empty():
            await asyncio.sleep(0.05)
        return time.monotonic() - t

    async def send(self, msg: dict) -> None:
        if self.closed:
            raise ChannelClosed(self.closed[1])
        await _maybe(self.ch.send(msg))

    def _close(self, outcome: str, why: str) -> None:
        self.closed = (outcome, why)
        for b in list(self.boxes.values()):
            b.q.put_nowait((_CLOSED, 0.0))

    async def reader(self) -> None:
        while True:
            try:
                m = await _maybe(self.ch.recv())
            except Reconnected:
                for b in list(self.boxes.values()):
                    b.q.put_nowait((_RESTART, 0.0))
                continue
            except ChannelClosed as e:
                return self._close("disconnected", f"the connection closed: {e}" if str(e) else "the connection closed")
            except Exception as e:
                return self._close("harness_error", f"the channel failed: {type(e).__name__}: {e}"[:500])
            key = m.get("scene_key") if isinstance(m, dict) else None
            b = self.boxes.get(key) if isinstance(key, str) else None
            if b is None:
                self.dropped += 1
                continue
            if b.q.qsize() >= self.max_queued:
                b.flood = True
                continue
            b.q.put_nowait((m, time.monotonic()))


async def arun(expert: dict, scenes: list[dict], *, time_limit_s: float, wall_s: float, workers: int = 1,
               docker: dict | None = None, io: dict | None = None, on_result=None, pull: bool = False,
               stderr_dir: str | Path | None = None, step_deadline_s: float | None = None,
               max_restarts: int | None = None, first_step_deadline_s: float | None = None,
               restarts_used: list[int] | None = None) -> list[dict]:
    if expert.get("policy") is not None:
        return await _policy_run(expert["policy"], scenes, time_limit_s=float(time_limit_s), wall_s=float(wall_s),
                                 workers=workers, on_result=on_result)
    if expert.get("remote") is None:
        return await asyncio.to_thread(run, expert, scenes, time_limit_s=time_limit_s, wall_s=wall_s, workers=workers,
                                       docker=docker, io=io, on_result=on_result, pull=pull, stderr_dir=stderr_dir)
    H = env_config()["harness"]
    R = H.get("remote") or {}
    deadline = float(step_deadline_s if step_deadline_s is not None else R.get("step_deadline_s", 2.0))
    first = float(first_step_deadline_s if first_step_deadline_s is not None else
                  R.get("first_step_deadline_s", max(deadline, 10.0)))
    restarts = int(max_restarts if max_restarts is not None else R.get("max_restarts", 2))
    max_abs = float(R.get("max_abs", 100.0))
    used = list(restarts_used or [])
    queued = int({**(H.get("io") or {}), **(io or {})}.get("max_queued") or 4)
    bundle = assets.ensure()
    mux = _Mux(expert["remote"], queued)
    out: list[dict | None] = [None] * len(scenes)
    todo = iter(range(len(scenes)))
    reader = asyncio.ensure_future(mux.reader())
    procs: list[NodeProc] = []

    async def worker() -> None:
        proc = NodeProc("harness.mjs", bundle)
        procs.append(proc)
        for i in todo:
            sc = scenes[i]
            n = max(0, int(used[i])) if i < len(used) and used[i] else 0
            while True:
                key = f"{i}.{n}"
                r, again = await _remote_scene(proc, mux, sc, key, time_limit_s=float(time_limit_s),
                                               wall_s=float(wall_s), deadline=deadline, first=first,
                                               last=n >= restarts, max_abs=max_abs)
                if not again:
                    break
                n += 1
            r["scene_key"], r["restarts"] = key, n
            if r.get("scene_id") is None:
                r["scene_id"] = sc.get("scene_id")
            r["infra"] = r.get("outcome") in INFRA_OUTCOMES
            out[i] = r
            if on_result:
                await _maybe(on_result(i, r))

    try:
        await asyncio.gather(*(worker() for _ in range(max(1, min(int(workers), len(scenes) or 1)))))
    finally:
        reader.cancel()
        try:
            await reader
        except (asyncio.CancelledError, Exception):
            pass
        for p in procs:
            await asyncio.to_thread(p.close)
    return [r if r is not None else _fail(scenes[i], "harness_error", "not run", 0.0) for i, r in enumerate(out)]


async def _remote_scene(proc: NodeProc, mux: _Mux, sc: dict, key: str, *, time_limit_s: float, wall_s: float,
                        deadline: float, first: float, last: bool, max_abs: float = 100.0) -> tuple[dict, bool]:
    call = lambda m: asyncio.to_thread(proc.call, m, wall_s + 120)
    box = mux.open(key)
    t0 = time.monotonic()

    async def stop(outcome: str, error: str, notify: bool = True) -> dict:
        r = (await call({"op": "stop", "outcome": outcome, "error": error}))["result"]
        r.pop("id", None)
        if notify:
            await _notify(r)
        return r

    async def _notify(r: dict) -> None:
        try:
            await mux.send({"type": "end", "scene_key": key, "outcome": r.get("outcome"), "t": r.get("t"),
                            "tick": r.get("tick"), "error": r.get("error")})
        except Exception:
            pass

    try:
        got = await call({"op": "open", "scene": _wire(sc), "time_limit_s": time_limit_s})
        if "result" in got:
            return got["result"], False
        msg = got["start"]
        while True:
            try:
                await mux.send({**msg, "scene_key": key})
            except ChannelClosed as e:
                outcome, why = mux.closed or ("disconnected", f"the connection closed: {e}" if str(e) else
                                              "the connection closed")
                return await stop(outcome, why, False), False
            sent = time.monotonic()
            cap, what = (first, "first-step deadline") if msg.get("type") == "start" else (deadline, "step deadline")
            while True:
                now = time.monotonic()
                left = wall_s - (now - t0)
                step = cap - (now - sent)
                wait = min(step, left)
                try:
                    if wait <= 0:
                        raise asyncio.TimeoutError
                    item, at = await asyncio.wait_for(box.q.get(), wait)
                    break
                except asyncio.TimeoutError:
                    if mux.paused():
                        held = await mux.resumed(box)
                        t0 += held
                        sent = time.monotonic()
                        continue
                    if step < left:
                        return await stop("step_timeout", f"no act within the {what} of {cap:g} s "
                                                          f"(tick {msg.get('tick')})"), False
                    return await stop("wall_timeout", f"no verdict within the wall budget of {wall_s:g} s"), False
            if item is _RESTART:
                if last:
                    return await stop("disconnected", "the connection was replaced while the scene ran, "
                                                      "and the scene has no restarts left", False), False
                await stop("disconnected", "restarted after a reconnect", False)
                return {}, True
            if item is _CLOSED:
                outcome, why = mux.closed or ("disconnected", "the connection closed")
                return await stop(outcome, why, False), False
            if box.flood:
                return await stop("expert_error", f"the expert sent more than {mux.max_queued} messages without "
                                                  "waiting for a state"), False
            why = act_error(item, max_abs)
            if why:
                return await stop("expert_error", why), False
            try:
                got = await call({"op": "act", "act": item, "wait_ms": max(0.0, (at - sent) * 1000)})
            except SimCrashed:
                raise
            except Exception as e:
                return await stop("expert_error", f"the act could not be applied: {type(e).__name__}: {e}"[:500]), False
            if "state" in got:
                msg = got["state"]
                continue
            r = got["result"]
            r.pop("id", None)
            if "end" in got:
                try:
                    await mux.send({**got["end"], "scene_key": key})
                except Exception:
                    pass
            else:
                await _notify(r)
            return r, False
    except Exception as e:
        await asyncio.to_thread(proc.close)
        return _fail(sc, "harness_error", f"{type(e).__name__}: {e}"[:500], time.monotonic() - t0), False
    finally:
        mux.drop(key)


async def _policy_run(spec: dict, scenes: list[dict], *, time_limit_s: float, wall_s: float, workers: int,
                      on_result) -> list[dict]:
    bundle = assets.ensure()
    out: list[dict | None] = [None] * len(scenes)
    todo = iter(range(len(scenes)))
    procs: list[NodeProc] = []

    async def worker() -> None:
        proc = NodeProc("harness.mjs", bundle)
        procs.append(proc)
        for i in todo:
            r = await _policy_scene(proc, spec, scenes[i], time_limit_s=time_limit_s, wall_s=wall_s)
            if r.get("scene_id") is None:
                r["scene_id"] = scenes[i].get("scene_id")
            r["infra"] = r.get("outcome") in INFRA_OUTCOMES
            out[i] = r
            if on_result:
                await _maybe(on_result(i, r))

    try:
        await asyncio.gather(*(worker() for _ in range(max(1, min(int(workers), len(scenes) or 1)))))
    finally:
        for p in procs:
            await asyncio.to_thread(p.close)
    return [r if r is not None else _fail(scenes[i], "harness_error", "not run", 0.0) for i, r in enumerate(out)]


async def _policy_scene(proc: NodeProc, spec: dict, sc: dict, *, time_limit_s: float, wall_s: float) -> dict:
    call = lambda m: asyncio.to_thread(proc.call, m, wall_s + 120)
    url, token = str(spec["url"]), str(spec.get("token") or "")
    calls, timeout = int(spec.get("calls") or 40), float(spec.get("timeout_s") or 60)
    episode = str(spec.get("episode") or "policy")
    t0 = time.monotonic()
    model = None

    async def stop(outcome: str, error: str) -> dict:
        r = (await call({"op": "stop", "outcome": outcome, "error": error[:500]}))["result"]
        r.pop("id", None)
        return r

    try:
        got = await call({"op": "open", "scene": _wire(sc), "time_limit_s": time_limit_s})
        if "result" in got:
            r = got["result"]
            r.pop("id", None)
            return r
        start = msg = got["start"]
        max_rows = int(start["limits"]["max_rows"])
        state, k = None, 0
        while True:
            left = max_rows - int(msg.get("tick") or 0)
            rows, waited = [], 0.0
            if k < calls and left > 0:
                budget = wall_s - (time.monotonic() - t0)
                if budget <= 0:
                    r = await stop("wall_timeout", f"no verdict within the wall budget of {wall_s:g} s")
                    break
                sent = time.monotonic()
                try:
                    j = await asyncio.to_thread(P.post, url, token, P.request(start, msg, k, state, episode),
                                                min(timeout, budget))
                    rows = [] if j.get("give_up") else P.rows(j.get("ctrl"))[:left]
                    why = act_error({"type": "act", "rows": rows})
                    if why:
                        raise ValueError(why)
                except P.PolicyUnavailable as e:
                    over = time.monotonic() - t0 >= wall_s
                    r = await stop("wall_timeout" if over else "policy_unavailable", str(e))
                    break
                except (P.PolicyRefused, TypeError, ValueError) as e:
                    r = await stop("expert_error", f"policy: {e}")
                    break
                waited = (time.monotonic() - sent) * 1000
                model = j.get("model") or model
                state = j.get("state")
                k += 1
            got = await call({"op": "act", "act": {"type": "act", "rows": rows, "obs": bool(rows)}, "wait_ms": waited})
            if "state" in got:
                msg = got["state"]
                continue
            r = got["result"]
            r.pop("id", None)
            break
    except Exception as e:
        await asyncio.to_thread(proc.close)
        return _fail(sc, "harness_error", f"{type(e).__name__}: {e}"[:500], time.monotonic() - t0)
    r["model"] = model
    return r


def compare(results_a: list[dict], results_b: list[dict]) -> dict:
    ids = lambda rs: [r.get("scene_id") for r in rs]
    by_id = all(None not in ids(rs) and len(set(ids(rs))) == len(rs) for rs in (results_a, results_b))
    if not by_id and len(results_a) != len(results_b):
        raise ValueError("scene ids are missing or repeated, so the results are paired by position, "
                         "and the two lists differ in length")
    keys = (lambda rs: ids(rs)) if by_id else (lambda rs: list(range(len(rs))))
    A, B = dict(zip(keys(results_a), results_a)), dict(zip(keys(results_b), results_b))
    rows = []
    for k in [k for k in keys(results_a) if k in B]:
        a, b = A[k], B[k]
        ha, hb = a.get("action_hash"), b.get("action_hash")
        rows.append({"scene_id": a.get("scene_id"), "equal": ha is not None and ha == hb, "a": ha, "b": hb,
                     "outcome_a": a.get("outcome"), "outcome_b": b.get("outcome"),
                     "infra": bool(a.get("outcome") in INFRA_OUTCOMES or b.get("outcome") in INFRA_OUTCOMES)})
    bad = [x for x in rows if not x["equal"]]
    return {"compared": len(rows), "equal": len(rows) - len(bad), "all_equal": bool(rows) and not bad,
            "mismatched": [x["scene_id"] for x in bad], "infra": sum(1 for x in bad if x["infra"]),
            "only_a": [k for k in keys(results_a) if k not in B], "only_b": [k for k in keys(results_b) if k not in A],
            "scenes": rows}
