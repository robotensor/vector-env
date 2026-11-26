import json
import shlex
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import HERE, fake, needs_bundle

from vector_env import cli, harness, scenes
from vector_env.compose import SceneError

ENV = {"version": "0.1.0", "assets_version": "2026.11.1", "assets_sha256": "e" * 64}


def fake_scene(seed: int, env: dict | None = None) -> dict:
    return {"scene_id": f"s{seed}-abcd0000", "seed": seed, "xml": "<mujoco/>", "meta": {"parts": [1, 2]},
            "checkpoint": "c", "state_hash": "h" * 16, "target": 0, "env": dict(env or ENV)}


def test_scene_cache_is_keyed_by_env_assets_seed_index_and_draw(tmp_path):
    c = scenes.SceneCache(tmp_path, ENV)
    seed0 = scenes.seed_for(3, 1)
    assert c.get(3, 1) is None
    assert c.put(3, 1, fake_scene(seed0 + 2), None)
    assert c.path(3, 1) == tmp_path / "0.1.0" / ("e" * 64) / scenes.draw_key(None) / "3" / "1.json"
    sc, err = c.get(3, 1)
    assert err is None and sc["seed"] == seed0 + 2
    assert c.get(3, 2) is None and c.get(4, 1) is None
    assert scenes.SceneCache(tmp_path, {**ENV, "version": "0.2.0"}).get(3, 1) is None
    assert scenes.SceneCache(tmp_path, {**ENV, "assets_sha256": "f" * 64}).get(3, 1) is None
    assert scenes.SceneCache(tmp_path, ENV, {"count": [2, 2]}).get(3, 1) is None
    assert scenes.draw_key({"count": [2, 2]}) != scenes.draw_key(None) == scenes.draw_key(scenes.default_draw())
    assert not c.put(3, 4, fake_scene(5, {**ENV, "assets_sha256": "0" * 64}), None)
    assert not c.put(3, 4, {"seed": 5}, None)
    assert not c.put(3, 4, None, RuntimeError("node is missing"))
    assert c.get(3, 4) is None
    assert c.put(3, 5, None, SceneError("could not place the drawn parts"))
    sc, err = c.get(3, 5)
    assert sc is None and isinstance(err, SceneError) and "could not place" in str(err)
    c.path(3, 1).write_text("{not json")
    assert c.get(3, 1) is None
    c.path(3, 1).write_text(json.dumps(fake_scene(seed0 + 9)))
    assert c.get(3, 1) is None
    assert not list(c.dir.rglob("*.tmp"))


def test_scene_cache_root(monkeypatch, tmp_path):
    monkeypatch.delenv("VECTOR_ENV_SCENE_CACHE")
    assert scenes.scene_cache_root() == Path.home() / ".cache" / "vector-env" / "scenes"
    monkeypatch.setenv("VECTOR_ENV_SCENE_CACHE", "off")
    assert scenes.scene_cache_root() is None
    assert scenes.scene_cache_root(tmp_path) == tmp_path
    assert cli.scene_cache(False) is None


def _slow_generator(monkeypatch, bad=()):
    calls, threads = [], set()

    def gen(seed, draw=None):
        calls.append(seed)
        threads.add(threading.get_ident())
        time.sleep(0.05 + (seed % 7) / 100)
        if seed in bad:
            raise SceneError("no part in the bin is visible enough to be a target")
        return fake_scene(seed)
    monkeypatch.setattr(scenes, "generate", gen)
    return calls, threads


def test_parallel_generation_keeps_the_order_and_reuses_cached_scenes(monkeypatch, tmp_path):
    bad = {scenes.seed_for(9, 2) + k for k in range(scenes.SEED_TRIES)}
    calls, threads = _slow_generator(monkeypatch, bad)
    cache = scenes.SceneCache(tmp_path, ENV)
    one, logs = [], []
    serial, skipped0 = cli.draw_scenes(8, 9, log=one.append)
    calls.clear()
    threads.clear()
    st = {}
    t0 = time.time()
    got, skipped = cli.draw_scenes(8, 9, log=logs.append, workers=4, cache=cache, stats=st)
    par_s = time.time() - t0
    assert [s["seed"] for s in got] == [s["seed"] for s in serial] and skipped == skipped0 == [2]
    assert logs == one and len(threads) == 4 and st.get("cached", 0) == 0
    assert par_s < 0.8 * sum(0.05 + (s % 7) / 100 for s in calls)
    calls.clear()
    logs.clear()
    again, skipped = cli.draw_scenes(8, 9, log=logs.append, workers=4, cache=cache, stats=st)
    assert calls == [] and st["cached"] == 8 and skipped == [2]
    assert [s["seed"] for s in again] == [s["seed"] for s in got]
    assert all(x.endswith("(cached)") for x in logs) and "scene 3/8: skipped" in logs[2]


def test_a_broken_generator_stops_the_parallel_draw(monkeypatch, tmp_path):
    def gen(seed, draw=None):
        raise RuntimeError("node: not found")
    monkeypatch.setattr(scenes, "generate", gen)
    cache = scenes.SceneCache(tmp_path, ENV)
    with pytest.raises(cli.EvalError, match="does not work here"):
        cli.draw_scenes(40, 1, log=lambda *x: None, workers=4, cache=cache)
    assert not list(tmp_path.rglob("*.json"))


def test_run_flags_reach_the_cache(monkeypatch, capsys, tmp_path):
    _slow_generator(monkeypatch)
    monkeypatch.setattr(cli, "scene_cache", lambda root, draw=None: None if root is False else
                        scenes.SceneCache(scenes.scene_cache_root(root), ENV, draw))
    monkeypatch.setattr(harness, "run", lambda ex, scs, **k: [
        {"scene_id": s["scene_id"], "outcome": "missed", "success": False, "t": 1.0, "n": 0, "ctrl": "", "wall_s": 0.1,
         "infra": False} for s in scs])
    d = tmp_path / "c"
    argv = ["run", "--command", "true", "--scenes", "3", "--seed", "2", "--workers", "3"]
    assert cli.main([*argv, "--scene-cache", str(d)]) == 0
    assert json.loads(capsys.readouterr().out)["scenes_cached"] == 0 and len(list(d.rglob("*.json"))) == 3
    assert cli.main([*argv, "--scene-cache", str(d)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["scenes_cached"] == 3 and out["scene_cache"].startswith(str(d))
    assert cli.main([*argv, "--no-scene-cache"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["scenes_cached"] == 0 and out["scene_cache"] is None


@needs_bundle
def test_run_trace_and_replay_trace(capsys, tmp_path):
    cmd = shlex.join([sys.executable, str(HERE / "fake_expert.py"), "empty"])
    tr, rows = tmp_path / "traces", tmp_path / "r.jsonl"
    assert cli.main(["run", "--command", cmd, "--scenes", "2", "--seed", "4", "--time-limit", "1", "--workers", "2",
                     "--trace", str(tr), "--results", str(rows)]) == 0
    err = capsys.readouterr().err
    res = [json.loads(x) for x in rows.read_text().splitlines()]
    files = sorted(tr.iterdir())
    assert [p.name for p in files] == [f"{i:04d}-{r['scene_id']}.json" for i, r in enumerate(res)]
    assert "traces: 2 of 2 scenes in" in err and err.count("why s") == 2
    t = json.loads(files[0].read_text())
    assert t["format"] == "vector-env-trace/1" and t["outcome"] == res[0]["outcome"] and t["result"]["n"] == res[0]["n"]
    assert len(t["ticks"]) == t["tick"] + 1 and t["ticks"][3]["tcp"] and len(t["ticks"][3]["tcp_quat"]) == 4
    assert {"held", "touch", "grip", "q", "row", "target"} <= set(t["ticks"][3])
    assert {"target_first_touch", "target_held", "grip_first_asked", "nearest_mm", "left_bin"} <= set(t["summary"])
    assert t["reasons"][0].startswith(f"{t['outcome']} at tick {t['tick']}")
    i = 1
    sc, _ = scenes.draw_index(4, i)
    (tmp_path / "scene.json").write_text(json.dumps(sc))
    base = ["replay", "--scene", str(tmp_path / "scene.json"), "--result", str(rows), "--time-limit", "1"]
    assert cli.main(base) == 0
    plain = json.loads(capsys.readouterr().out)
    one = tmp_path / "one.json"
    assert cli.main([*base, "--trace", str(one)]) == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out) == plain and cap.err.startswith("why: ")
    assert json.loads(one.read_text())["reasons"] == json.loads(files[i].read_text())["reasons"]


@needs_bundle
def test_a_trace_names_the_harness_outcome(scene):
    r = harness.run(fake("crash"), [scene], time_limit_s=2, wall_s=60)[0]
    assert r["outcome"] == "expert_error"
    t = harness.trace(scene, r["ctrl"], r["n"], time_limit_s=2, result=r)
    assert t["reasons"][0].startswith("the harness ended the scene as expert_error") and t["rows"] == 0
    with pytest.raises(harness.ReplayMismatch):
        harness.trace(scene, r["ctrl"], r["n"], time_limit_s=2, result={**r, "state_hash": "x"})
