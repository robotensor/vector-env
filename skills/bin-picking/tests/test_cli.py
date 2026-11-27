import json
import shlex
import sys

import pytest
from conftest import HERE, fake, needs_bundle

from vector_env import cli


@needs_bundle
def test_run_prints_the_score(capsys):
    cmd = shlex.join([sys.executable, str(HERE / "fake_expert.py"), "empty"])
    assert cli.main(["run", "--draw", '{"mode": "classic"}', "--command", cmd, "--scenes", "2", "--seed", "4", "--time-limit", "3", "--workers", "2"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["scenes"] == 2 and out["score"] == 0.0 and out["time_limit_s"] == 3


@needs_bundle
def test_scene_summary(capsys):
    assert cli.main(["scene", "--draw", '{"mode": "classic"}', "--seed", "2", "--summary"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["seed"] == 2 and out["parts"] >= 1 and out["env"]["assets_sha256"]


def _fake_scenes(n):
    return [{"scene_id": f"s{i}-0000000{i}", "seed": i, "xml": "<mujoco/>", "meta": {"parts": [1]}, "checkpoint": "",
             "state_hash": f"{i}" * 8, "target": 1} for i in range(n)]


def test_draw_scene_uses_the_round_retry_rule():
    from vector_env import scenes
    from vector_env.assets import AssetsError
    from vector_env.compose import SceneError
    calls = []

    def gen(seed, draw):
        calls.append(seed)
        if seed < 102:
            raise SceneError("no part in the bin is visible enough to be a target")
        return {"seed": seed}
    assert scenes.draw_scene(100, None, generate_fn=gen) == ({"seed": 102}, None) and calls == [100, 101, 102]
    calls.clear()
    sc, err = scenes.draw_scene(0, None, generate_fn=gen)
    assert sc is None and isinstance(err, SceneError) and calls == list(range(scenes.SEED_TRIES)) and scenes.SEED_TRIES == 6
    calls.clear()
    sc, _ = scenes.draw_index(3, 2, None, salt="pepper", generate_fn=lambda s, d: {"seed": s})
    assert sc["seed"] == scenes.seed_for(3, 2, "pepper")

    def gone(seed, draw):
        raise AssetsError("no bundle")
    with pytest.raises(AssetsError):
        scenes.draw_scene(0, None, generate_fn=gone)


def test_run_skips_an_index_whose_draws_place_no_scene(monkeypatch, capsys):
    from vector_env import harness, scenes
    from vector_env.compose import SceneError
    bad = {scenes.seed_for(5, 1) + k for k in range(scenes.SEED_TRIES)}
    retry = scenes.seed_for(5, 2)

    def gen(seed, draw):
        if seed in bad or seed == retry:
            raise SceneError("no part in the bin is visible enough to be a target")
        return {**_fake_scenes(1)[0], "scene_id": f"s{seed}", "seed": seed}
    monkeypatch.setattr(scenes, "generate", gen)
    ran = []

    def run(expert, scs, **k):
        ran.extend(scs)
        return [{"scene_id": s["scene_id"], "outcome": "missed", "success": False, "t": 1.0, "n": 20, "wall_s": 0.1,
                 "error": None, "infra": False} for s in scs]
    monkeypatch.setattr(harness, "run", run)
    assert cli.main(["run", "--draw", '{"mode": "classic"}', "--command", "true", "--scenes", "4", "--seed", "5"]) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out.strip().splitlines()[-1])
    assert out["skipped"] == [1] and out["scenes"] == 3 and out["seed_tries"] == 6
    assert [s["seed"] for s in ran] == [scenes.seed_for(5, 0), retry + 1, scenes.seed_for(5, 3)]
    assert "scene 2/4: skipped" in cap.err and "Traceback" not in cap.err


def test_a_broken_generator_is_one_line(monkeypatch, capsys):
    from vector_env import scenes
    from vector_env.node import SimError

    def gen(seed, draw):
        raise SimError("sim.mjs stopped")
    monkeypatch.setattr(scenes, "generate", gen)
    assert cli.main(["run", "--draw", '{"mode": "classic"}', "--command", "true", "--scenes", "3"]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert err[-1].startswith("vector-env: the scene generator does not work here")

    def nothing(seed, draw):
        from vector_env.compose import SceneError
        raise SceneError("could not place the drawn parts")
    monkeypatch.setattr(scenes, "generate", nothing)
    assert cli.main(["scene", "--draw", '{"mode": "classic"}', "--seed", "9"]) == 2
    assert "no valid scene" in capsys.readouterr().err
    assert cli.main(["run", "--draw", '{"mode": "classic"}', "--command", "true", "--scenes", "2"]) == 2
    assert "none of the 2 indexes" in capsys.readouterr().err


def test_image_without_docker_is_not_a_score(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "draw_scenes", lambda n, seed, draw=None, log=None, **k: (_fake_scenes(n), []))
    rows = tmp_path / "r.jsonl"
    assert cli.main(["run", "--draw", '{"mode": "classic"}', "--image", "ghcr.io/a/b:1", "--docker", str(tmp_path / "no-docker"), "--scenes", "2",
                     "--results", str(rows)]) == 2
    cap = capsys.readouterr()
    assert cap.out == "" and "docker_unavailable" in cap.err and "--command" in cap.err and "not a score" in cap.err
    assert len(rows.read_text().splitlines()) == 2


def test_partial_infra_warns(capsys):
    res = [{"scene_id": "a", "outcome": "success", "success": True, "t": 2.0, "n": 40, "wall_s": 1, "infra": False},
           {"scene_id": "b", "outcome": "harness_error", "success": False, "t": 0.0, "n": 0, "wall_s": 0, "infra": True}]
    from vector_env import harness
    assert cli.report({"summary": harness.summary(res), "results": res}, None) == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["infra"] == 1 and "1 of 2 scenes did not run here (harness_error)" in cap.err


def test_replay_refuses_a_result_of_another_scene(monkeypatch, capsys, tmp_path):
    from vector_env import harness
    a, b = _fake_scenes(2)
    (tmp_path / "a.json").write_text(json.dumps(a))
    (tmp_path / "res.json").write_text(json.dumps({"scene_id": b["scene_id"], "ctrl": "", "n": 0}))
    assert cli.main(["replay", "--scene", str(tmp_path / "a.json"), "--result", str(tmp_path / "res.json")]) == 2
    assert "not s0-00000000" in capsys.readouterr().err
    seen = []

    class Sim:
        def call(self, msg):
            seen.append(msg["scene"]["scene_id"])
            return {"outcome": "missed", "success": False, "t": 0.0}
    monkeypatch.setattr(harness, "sim", lambda bundle: Sim())
    monkeypatch.setattr(harness.assets, "ensure", lambda: tmp_path)
    lines = [{"scene_id": x["scene_id"], "ctrl": "", "n": 0} for x in (b, a)]
    (tmp_path / "rows.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    assert cli.main(["replay", "--scene", str(tmp_path / "a.json"), "--result", str(tmp_path / "rows.jsonl")]) == 0
    assert seen == [a["scene_id"]] and json.loads(capsys.readouterr().out)["outcome"] == "missed"
    (tmp_path / "rows.jsonl").write_text((json.dumps(lines[0]) + "\n") * 2)
    assert cli.main(["replay", "--scene", str(tmp_path / "a.json"), "--result", str(tmp_path / "rows.jsonl")]) == 2
    assert "has no result for scene s0-00000000" in capsys.readouterr().err
    assert cli.main(["replay", "--scene", str(tmp_path / "nope.json"), "--result", str(tmp_path / "rows.jsonl")]) == 2
    with pytest.raises(harness.ReplayMismatch):
        harness.check_replay(a, {"state_hash": "x"})


@needs_bundle
def test_documented_eval_does_not_crash(capsys):
    cmd = shlex.join([sys.executable, str(HERE / "fake_expert.py"), "empty"])
    assert cli.main(["run", "--draw", '{"mode": "classic"}', "--command", cmd, "--scenes", "7", "--seed", "1", "--time-limit", "0.5", "--workers", "4"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["scenes"] + len(out["skipped"]) == 7


@needs_bundle
def test_stderr_dir(scene, tmp_path):
    from vector_env import harness
    d = tmp_path / "logs"
    r = harness.run(fake("noisy"), [scene], time_limit_s=1, wall_s=60, stderr_dir=d)[0]
    assert r["error"] is None
    logs = list(d.iterdir())
    assert [p.name for p in logs] == [f"0000-{scene['scene_id']}.log"] and "noisy expert says hi" in logs[0].read_text()
