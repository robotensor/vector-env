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
    assert t["time_limit_s"] == 15 and t["human_limit_s"] == 20 and t["max_rows"] == 300 and t["control_hz"] == 20
    assert rules.task(10)["human_limit_s"] == 20 and rules.task(30)["human_limit_s"] == 30
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


def _fake_pile(monkeypatch, *levels, parts=100):
    """scenes.generate with a stub composer and simulator: pile scenes whose settles report `levels` in turn."""
    R = {"level_range": [0.5, 0.667], "level_tol": 0.02, "count_range": [50, 150]}
    sc = {"settings": {"mode": "pile"}, "xml": "<mujoco/>", "meta": {"config": {"rain": R}}}
    todo = list(levels)

    class Sim:
        def call(self, msg):
            return {"parts": parts, "fill": {"level": todo.pop(0), "asleep": True, "spawned": parts}, "target": 0,
                    "target_class": "top", "checkpoint": "c", "state_hash": "ab" * 8}

    monkeypatch.setattr(scenes.assets, "resolve", lambda b=None: b)
    monkeypatch.setattr(scenes, "_compose", lambda st, seed, b: copy.deepcopy(sc))
    monkeypatch.setattr(scenes, "sim", lambda b: Sim())
    monkeypatch.setattr(scenes, "env_info", lambda b=None: {"version": "x"})
    return lambda: scenes.generate(3, settings={"mode": "pile"})


@pytest.mark.parametrize("level", [0.5 - 0.02, 0.5, 0.6, 0.667, 0.667 + 0.02])
def test_pile_level_within_range_is_kept(monkeypatch, level):
    assert _fake_pile(monkeypatch, level)()["pile"]["level"] == level


@pytest.mark.parametrize("level", [0.47, 0.69, 0.8])
def test_pile_level_outside_range_is_refused(monkeypatch, level):
    with pytest.raises(SceneError, match="fills"):
        _fake_pile(monkeypatch, level)()


def test_an_overfull_pile_tries_the_next_seed(monkeypatch):
    _fake_pile(monkeypatch, 0.75, 0.69, 0.6)
    seen = []
    sc, err = scenes.draw_scene(10, generate_fn=lambda seed, draw=None, **kw: scenes.generate(seed, settings={"mode": "pile"}),
                                on_error=lambda k, e: seen.append(str(e)))
    assert err is None and sc["seed"] == 12 and sc["pile"]["level"] == 0.6
    assert seen == ["the pile fills 0.75 of the crate (needs 0.5-0.667)", "the pile fills 0.69 of the crate (needs 0.5-0.667)"]


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
    B = compose({**st, "bury": True}, 4)["meta"]["fill"]["bury"]
    from vector_env.skills.bin_picking.settings import cell_config
    R = cell_config()["rain"]["bury"]
    assert len(B["parts"]) == min(R["parts"], len(M["parts"]) // 4) and len(B["quats"]) == len(B["parts"])
    assert B["ring_n"] == min(R["ring_n"], len(B["parts"]) - 1) and B["cap"] == R["cap"] and B["ring"] == R["ring"]
    assert B["undo_failed"] is True   # a bury whose last try fails takes its parts back out of the bin
    assert M["config"]["rules"]["target"]["hidden_max_depth"] == 2
    b = [s["batch"] for s in M["fill"]["spawns"]]
    assert b[0] == 0 and all(0 <= y - x <= 1 for x, y in zip(b, b[1:]))
    with pytest.raises(SceneError, match="level"):
        compose({**st, "level": 0.9}, 4)
    old = compose({"parts": {"gear": 2}}, 1)
    assert "sleep" not in old["xml"] and "rain" not in old["meta"]["config"] and "mode" not in old["meta"]["config"]["rules"]
