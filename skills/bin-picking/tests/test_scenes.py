import copy

import pytest

from conftest import needs_bundle

from vector_env import rules, scenes
from vector_env.compose import SceneError, compose
from vector_env.settings import cell_config

KEYS = {"scene_id", "seed", "xml", "meta", "checkpoint", "state_hash", "target", "env"}


def test_generate_is_reproducible(scene):
    again = scenes.generate(1)
    assert set(scene) == KEYS
    assert again["state_hash"] == scene["state_hash"] and again["xml"] == scene["xml"]
    assert again["checkpoint"] == scene["checkpoint"] and again["target"] == scene["target"]
    assert scene["env"]["version"] and len(scene["env"]["assets_sha256"]) == 64
    assert scene["meta"]["config"]["rules"]["try_s"] == rules.defaults()["time_limit_s"]
    assert scenes.verify(scene)


def test_verify_catches_changes(scene):
    for k, v in (("state_hash", "0" * 16), ("target", 999)):
        bad = copy.deepcopy(scene)
        bad[k] = v
        assert not scenes.verify(bad)
    bad = copy.deepcopy(scene)
    del bad["checkpoint"]
    assert not scenes.verify(bad)


def test_different_seeds_differ(scenes3):
    assert len({s["state_hash"] for s in scenes3}) == 3
    assert len({s["scene_id"] for s in scenes3}) == 3
    assert scenes.seed_for(7, 0) != scenes.seed_for(7, 1) and scenes.seed_for(7, 0) == scenes.seed_for(7, 0)


def test_draw_follows_settings():
    D = scenes.default_draw()
    for seed in range(5):
        st = scenes.settings_for(seed)
        n = sum(st["parts"].values())
        assert D["count"][0] <= n <= D["count"][1] and 1 <= len(st["parts"]) <= D["kinds"][1]
        assert st == scenes.settings_for(seed)


def test_task_rules():
    t = rules.task()
    assert t["time_limit_s"] == 15 and t["human_limit_s"] == 30 and t["max_rows"] == 300 and t["control_hz"] == 20
    t = rules.task(10, 3)
    assert t["human_limit_s"] == 30 and t["max_rows"] == 200
    with pytest.raises(ValueError):
        rules.task(0)
    assert cell_config()["rules"]["try_s"] == 15


@needs_bundle
def test_composer_refusals():
    from vector_env import assets, library
    big = {"crate": "s", "random": [{"filter": {"size_mm": [90, 200]}, "kinds": 5, "count": 25}]}
    with pytest.raises(SceneError, match=r"at most \d+ parts of these sizes fit in this crate \(s: .*at most 2 layers"):
        compose(big, 1)
    con, _ = library._db(assets.bundle_dir())
    small = con.execute("select id from assets where size_mm < 30 order by id limit 1").fetchone()[0]
    with pytest.raises(SceneError, match=r"too small for the cell \(largest side \d+ mm < 30 mm\)"):
        compose({"parts": {small: 1}}, 1)
    drawn = compose({"crate": "m", "random": [{"filter": {"size_mm": [0, 35]}, "kinds": 6, "count": 6}]}, 3)["meta"]
    assert all(max(t["extents"]) >= 0.0299 and t["mass"] >= 0.01 for t in drawn["part_types"].values())
    with pytest.raises(SceneError, match="unknown crate 'pile'"):
        compose({"crate": "pile", "parts": {"gear": 2}}, 1)
    with pytest.raises(SceneError, match="0-30 copies|1-30 parts"):
        compose({"parts": {"gear": 31}}, 1)
    assert compose({"parts": {"gear": 2}}, 1)["meta"]["crate"]["id"] == "m"
