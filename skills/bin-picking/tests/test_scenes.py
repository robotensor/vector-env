import copy

import pytest

from conftest import needs_bundle

from vector_env import rules, scenes
from vector_env.compose import SceneError, compose
from vector_env.settings import cell_config

KEYS = {"scene_id", "seed", "xml", "meta", "checkpoint", "state_hash", "target", "env"}


def test_generate_is_reproducible(scene):
    again = scenes.generate(1, {"mode": "classic"})
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
    assert scenes.default_draw()["mode"] == "pile"
    D = {**scenes.default_draw(), "mode": "classic"}
    for seed in range(5):
        st = scenes.settings_for(seed, D)
        n = sum(st["parts"].values())
        assert D["count"][0] <= n <= D["count"][1] and 1 <= len(st["parts"]) <= D["kinds"][1]
        assert st == scenes.settings_for(seed, D)


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


def test_pile_draw():
    D = scenes.pile_draw()
    for seed in range(8):
        st = scenes.settings_for(seed, {"mode": "pile"})
        assert st == scenes.settings_for(seed, {"mode": "pile"})
        assert st["mode"] == "pile" and st["crate"] in D["crate"]
        assert D["level"][0] <= st["level"] <= D["level"][1]
        assert 1 <= len(st["parts"]) <= D["kinds"][1] and sum(st["parts"].values()) <= D["pool_max"]
    assert scenes.settings_for(0) == scenes.settings_for(0, {"mode": "pile"}) != scenes.settings_for(0, {"mode": "classic"})


def test_pile_draw_repeats_kinds_of_a_small_library(monkeypatch):
    rows = (("a", 2e-5), ("b", 4e-5), ("c", 3e-5))
    monkeypatch.setattr(scenes, "_pile_rows", lambda *a: rows)
    kinds = set()
    for seed in range(12):
        st = scenes.settings_for(seed, {"mode": "pile"})
        assert set(st["parts"]) <= {"a", "b", "c"} and sum(st["parts"].values()) <= 300
        kinds.add(len(st["parts"]))
    assert max(kinds) == 3


@needs_bundle
def test_pile_compose():
    st = scenes.settings_for(4, {"mode": "pile"})
    sc = compose(st, 4)
    M = sc["meta"]
    assert M["fill"]["mode"] == "rain" and M["fill"]["level"] == st["level"]
    from vector_env.skills.bin_picking.settings import env_config
    T = env_config()["task"]["pile"]
    assert M["config"]["rules"]["mode"] == "pile" and M["config"]["rules"]["try_s"] == T["time_limit_s"]
    assert M["config"]["rules"]["picks"] == T.get("picks", 10)
    assert M["config"]["physics"]["sleep"] and '<flag sleep="enable"/>' in sc["xml"]
    bury = (M["fill"].get("bury") or {}).get("parts", [])
    assert bool(bury) == bool(st.get("bury"))
    assert len(M["parts"]) == sum(st["parts"].values()) == len(M["fill"]["spawns"]) + len(bury)
    assert sorted([s["part"] for s in M["fill"]["spawns"]] + bury) == list(range(len(M["parts"])))
    b = [s["batch"] for s in M["fill"]["spawns"]]
    assert b[0] == 0 and all(0 <= y - x <= 1 for x, y in zip(b, b[1:]))
    with pytest.raises(SceneError, match="level"):
        compose({**st, "level": 0.9}, 4)
    old = compose({"parts": {"gear": 2}}, 1)
    assert "sleep" not in old["xml"] and "rain" not in old["meta"]["config"] and "mode" not in old["meta"]["config"]["rules"]
