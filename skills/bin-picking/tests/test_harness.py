import copy
import json
import os
import sys
import time
from pathlib import Path

import pytest
from conftest import HERE, ROOT, fake

from vector_env import harness, rules

EXAMPLE = {"command": ["node", str(ROOT / "skills" / "bin-picking" / "example" / "solve.mjs")]}
RESULT_KEYS = {"scene_id", "outcome", "success", "t", "ctrl", "n", "wall_s", "error"}


def test_the_example_expert_runs_over_several_scenes(scenes3):
    seen = []
    res = harness.run(EXAMPLE, scenes3, time_limit_s=15, wall_s=60, workers=2, on_result=lambda i, r: seen.append(i))
    assert sorted(seen) == [0, 1, 2]
    for sc, r in zip(scenes3, res):
        assert RESULT_KEYS <= set(r) and r["scene_id"] == sc["scene_id"]
        assert r["outcome"] in ("success", "missed", "slipped", "knocked_out", "timed_out", "wrong_part"), r
        assert r["n"] == round(r["t"] / 0.05) and r["error"] is None
        rp = harness.replay(sc, r["ctrl"], r["n"], time_limit_s=15)
        assert (rp["outcome"], rp["success"], rp["t"]) == (r["outcome"], r["success"], r["t"])
    s = harness.summary(res)
    assert s["scenes"] == 3 and s["successes"] == sum(r["success"] for r in res), s
    for r in res:
        assert {"outs", "out_parts", "tcp_path_m", "joint_path_rad", "crate_hits", "target_class", "score"} <= set(r), r
        assert r["score"] == rules.score(r, rules.task(15))
    assert s["counted"] == 3 and s["outs"] == sum(r["outs"] for r in res)
    assert s["mean_score"] == round(sum(r["score"] for r in res) / 3, 6)


def test_scene_score_and_summary():
    t = rules.task(30)
    assert t["score"] == {"time_weight": 0.5, "path_weight": 0.0, "path_ref_m": 3.0, "out_unit": 1.0}
    assert rules.score({"outcome": "success", "t": 0, "outs": 0}, t) == 1.0
    assert rules.score({"outcome": "success", "t": 15, "outs": 0}, t) == 0.75
    assert rules.score({"outcome": "success", "t": 99, "outs": 0}, t) == 0.5
    assert rules.score({"outcome": "success", "t": 15, "outs": 3}, t) == 0.75 - 7
    assert rules.score({"outcome": "timed_out", "t": 30, "outs": 1}, t) == -1.0
    assert rules.score({"outcome": "slipped", "t": 3, "outs": 0}, t) == 0.0
    assert rules.score({"outcome": "expert_error", "outs": 2}, t) == -3.0
    assert rules.score({"outcome": "harness_error"}, t) is None
    assert rules.score({"outcome": "knocked_out", "outs": 99}, t) == -(2 ** 30 - 1)
    p = rules.task(30, score={"path_weight": 0.2, "path_ref_m": 2.0})
    assert rules.score({"outcome": "success", "t": 0, "tcp_path_m": 1.0}, p) == 0.9
    assert rules.score({"outcome": "success", "t": 0, "tcp_path_m": 5.0}, p) == 0.8
    with pytest.raises(ValueError):
        rules.task(30, score={"time_weight": 2})
    assert rules.defaults("pile")["time_limit_s"] == 15 and rules.defaults()["time_limit_s"] == 15
    res = [{"outcome": "success", "success": True, "t": 6.0, "outs": 0, "score": 0.9},
           {"outcome": "timed_out", "success": False, "t": 30.0, "outs": 2, "score": -3.0},
           {"outcome": "harness_error", "success": False, "t": 0.0, "infra": True, "score": None}]
    s = harness.summary(res)
    assert (s["counted"], s["mean_score"], s["success_rate"], s["mean_t_s"], s["outs"]) == (2, -1.05, 0.5, 18.0, 2), s
    f = harness._fail({"scene_id": "x", "target_class": "hidden"}, "expert_error", "boom", 0.0)
    assert (f["outs"], f["out_parts"], f["crate_hits"], f["target_class"], f["score"]) == (0, [], 0, "hidden", 0.0), f
    assert harness._fail({}, "harness_error", "x", 0.0)["score"] is None


def test_robot_time_limit_and_pipe_overhead(scene):
    r = harness.run(fake("hold"), [scene], time_limit_s=5, wall_s=60)[0]
    assert (r["outcome"], r["t"], r["n"]) == ("timed_out", 5, 100)
    assert r["exchanges"] == 100
    ex = r["exchange"]
    print(f"\npipe: {ex['n']} exchanges, mean {ex['mean_ms']} ms, p50 {ex['p50_ms']} ms, max {ex['max_ms']} ms; "
          f"sim {r['sim_ms']} ms total, setup {r['setup_s']} s")
    assert ex["p50_ms"] < 20
    rp = harness.replay(scene, r["ctrl"], r["n"], time_limit_s=5)
    assert (rp["outcome"], rp["t"]) == ("timed_out", 5)
    r = harness.run(fake("hold", "10"), [scene], time_limit_s=15, wall_s=60)[0]
    assert (r["outcome"], r["n"], r["exchanges"]) == ("timed_out", 300, 30)


def test_empty_act_holds_until_the_rules_decide(scene):
    r = harness.run(fake("empty"), [scene], time_limit_s=15, wall_s=60)[0]
    assert r["outcome"] in ("timed_out", "missed") and r["n"] == 300 and r["exchanges"] == 1
    assert harness.replay(scene, r["ctrl"], r["n"], time_limit_s=15)["outcome"] == r["outcome"]


def test_observation_on_request(scene):
    r = harness.run(fake("obs"), [scene], time_limit_s=15, wall_s=60)[0]
    assert r["outcome"] == "expert_error" and "exited (0)" in r["error"]


@pytest.mark.parametrize("mode,match", [("crash", "exited (3)"), ("junk", "not JSON"), ("error", "cannot plan"),
                                        ("badrow", "7 finite numbers")])
def test_bad_experts(scene, mode, match):
    r = harness.run(fake(mode), [scene], time_limit_s=15, wall_s=30)[0]
    assert r["outcome"] == "expert_error" and not r["success"] and match in r["error"], r
    assert r.get("exit_code") == (3 if mode == "crash" else None), r


@pytest.mark.parametrize("mode,match", [("longline", "longer than 1024 KiB"), ("nonl", "longer than 1024 KiB"),
                                        ("flood", "more than 4 messages without waiting")])
def test_hostile_output_is_bounded(scene, mode, match):
    r = harness.run(fake(mode), [scene], time_limit_s=15, wall_s=30)[0]
    assert r["outcome"] == "expert_error" and match in r["error"] and r["wall_s"] < 10, r


def test_output_budget_is_configurable(scene):
    r = harness.run(fake("hold"), [scene], time_limit_s=15, wall_s=30, io={"max_read_mib": 0.01})[0]
    assert r["outcome"] == "expert_error" and "wrote more than" in r["error"], r


def _gone(pid: int, wait_s: float = 30) -> bool:
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        stat = Path(f"/proc/{pid}/stat")
        try:
            if stat.read_text().rsplit(")", 1)[1].split()[0] in ("Z", "X"):
                return True
        except (OSError, IndexError):
            pass
        time.sleep(0.05)
    return False


def test_wall_budget(scene, tmp_path):
    pidfile = tmp_path / "pid"
    t0 = time.monotonic()
    r = harness.run(fake("sleep", str(pidfile)), [scene], time_limit_s=15, wall_s=4)[0]
    took = time.monotonic() - t0
    assert r["outcome"] == "wall_timeout" and "wall budget of 4" in r["error"] and not r["success"], r
    assert 3.9 <= r["wall_s"] <= took and r["setup_s"] <= r["wall_s"], r
    assert took < 4 + 2 + 60, took
    assert r["t"] == 0 and r["n"] == 0 and r["exchanges"] == 0, r
    assert not pidfile.exists() or _gone(int(pidfile.read_text())), "the expert outlived its wall budget"


def test_scene_is_checked(scene):
    bad = {**scene, "state_hash": "0" * 16}
    r = harness.run(fake("hold"), [bad], time_limit_s=15, wall_s=30)[0]
    assert r["outcome"] == "scene_error" and "state hash" in r["error"]


def test_docker_mode(scene, tmp_path):
    argv, kill = harness.docker_argv("ghcr.io/a/b@sha256:" + "0" * 64, "n1", {"cpus": 2, "memory": "4g"}, tmp_path)
    assert argv[:4] == ["docker", "run", "--rm", "-i"] and argv[-1].startswith("ghcr.io/a/b")
    for flag, v in (("--network", "none"), ("--cpus", "2"), ("--memory", "4g"), ("--memory-swap", "4g"), ("--name", "n1"),
                    ("--pull", "never"), ("--pids-limit", "256"), ("--cap-drop", "ALL"), ("--user", "65534:65534"),
                    ("--security-opt", "no-new-privileges"), ("--tmpfs", "/tmp:rw,nosuid,nodev,size=1024m"),
                    ("-v", f"{tmp_path}:/bundle:ro")):
        assert argv[argv.index(flag) + 1] == v
    assert "--read-only" in argv and kill == ["docker", "kill", "n1"] and "--userns" not in argv
    assert harness.docker_argv("i", "n2", {"user": None, "userns": "host"})[0].count("--user") == 0
    assert "--userns" in harness.docker_argv("i", "n2", {"userns": "host"})[0]
    shim = tmp_path / "docker"
    shim.write_text(f"#!/bin/sh\nexec {sys.executable} {HERE / 'fake_docker.py'} \"$@\"\n")
    shim.chmod(0o755)
    D = {"binary": str(shim)}
    r = harness.run({"image": "fake:hold-20"}, [scene], time_limit_s=5, wall_s=30, docker=D)[0]
    assert (r["outcome"], r["n"]) == ("timed_out", 100)
    r = harness.run({"image": "fake:sleep"}, [scene], time_limit_s=5, wall_s=3, docker=D)[0]
    assert r["outcome"] == "wall_timeout"


def _shim(tmp_path, state=None):
    shim = tmp_path / "docker"
    shim.write_text(f"#!/bin/sh\nexec {sys.executable} {HERE / 'fake_docker.py'} \"$@\"\n")
    shim.chmod(0o755)
    if state is not None:
        (tmp_path / "state.json").write_text(json.dumps(state))
    return {"binary": str(shim)}


def _state(tmp_path):
    return json.loads((tmp_path / "state.json").read_text())


def test_infrastructure_failures_are_not_expert_failures(scene, tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(tmp_path / "state.json"))
    D = _shim(tmp_path, {"daemon": True, "local": {"fake:hold-20": 1000, "fake:nostart": 1000, "fake:exit125": 1000}})
    one = lambda image, **k: harness.run({"image": image}, [scene], time_limit_s=5, wall_s=20, docker=D, **k)[0]
    r = one("fake:nostart")
    assert (r["outcome"], r["infra"]) == ("start_failed", True) and "not created" in r["error"], r
    r = one("fake:exit125")
    assert (r["outcome"], r["infra"]) == ("expert_error", False) and "exited (125)" in r["error"], r
    r = one("fake:gone")
    assert (r["outcome"], r["infra"], r["wall_s"]) == ("image_missing", True, 0.0), r
    r = one("fake:hold-20")
    assert (r["outcome"], r["infra"]) == ("timed_out", False), r
    s = harness.summary([r, one("fake:nostart")])
    assert s["infra"] == 1 and s["outcomes"] == {"start_failed": 1, "timed_out": 1}
    r = harness.run({"image": "fake:hold"}, [scene, scene], time_limit_s=5, wall_s=20, docker={"binary": str(tmp_path / "none")})
    assert [x["outcome"] for x in r] == ["docker_unavailable"] * 2 and all(x["infra"] for x in r)
    _shim(tmp_path, {"daemon": False})
    assert one("fake:hold-20")["outcome"] == "docker_unavailable"
    r = harness.run(fake("hold"), [scene], time_limit_s=1, wall_s=20)[0]
    assert r["infra"] is False
    r = harness.run({"command": [str(tmp_path / "missing-binary")]}, [scene], time_limit_s=1, wall_s=20)[0]
    assert (r["outcome"], r["infra"]) == ("start_failed", True), r
    assert not list(tmp_path.glob("vector-env-cid-*"))


def test_image_size_limit_before_and_after_pull(scene, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(tmp_path / "state.json"))
    G = 1 << 30
    D = {**_shim(tmp_path, {"daemon": True, "local": {},
                            "remote": {"fake:hold-20": G // 2, "fake:big": 3 * G, "fake:bomb": G, "fake:flaky": G // 2},
                            "unpacked": {"fake:bomb": 9 * G}, "pull_fail": ["fake:flaky"]}),
         "max_image_gb": 2, "max_unpacked_gb": 6}
    one = lambda image: harness.run({"image": image}, [scene], time_limit_s=5, wall_s=20, docker=D, pull=True)[0]
    r = one("fake:big")
    assert (r["outcome"], r["infra"]) == ("image_too_large", False) and "3.00 GB compressed" in r["error"], r
    assert ["pull", "fake:big"] not in _state(tmp_path)["calls"]
    r = one("fake:bomb")
    assert r["outcome"] == "image_too_large" and "unpacked" in r["error"], r
    assert "fake:bomb" not in _state(tmp_path)["local"] and ["image", "rm"] in _state(tmp_path)["calls"]
    assert (one("fake:flaky")["outcome"], one("fake:nope")["outcome"]) == ("pull_failed", "pull_failed")
    assert one("fake:hold-20")["outcome"] == "timed_out"
    n = len(_state(tmp_path)["calls"])
    assert one("fake:hold-20")["outcome"] == "timed_out" and len(_state(tmp_path)["calls"]) == n
    with pytest.raises(harness.ImageTooLarge):
        harness.pull("fake:big", D)
    with pytest.raises(harness.PullFailed):
        harness.pull("fake:flaky", D)
    assert harness.remove_image("fake:hold-20", D) and harness.image_size("fake:hold-20", D) is None
    assert harness.manifest_size("fake:hold-20", D) == G // 2


def _picks_scene(scene, picks=3):
    sc = copy.deepcopy(scene)
    sc["meta"]["config"]["rules"].update(mode="pile", picks=picks, target={"min_px": 10, "buried_min_px": 5})
    sc["scene_id"] = scene["scene_id"] + "-picks"
    return sc


def test_a_multi_pick_scene_runs_replays_and_scores(scene):
    from vector_env import verifier
    sc = _picks_scene(scene)
    assert rules.scene_picks(sc) == 3 and rules.scene_picks(scene) == 1
    r = harness.run(fake("hold"), [sc], time_limit_s=1, wall_s=20)[0]
    assert (r["outcome"], r["picks_planned"], len(r["picks"]), r["n"], r["tick"], r["t"]) == ("done", 3, 3, 60, 60, 3.0), r
    assert [p["pick"] for p in r["picks"]] == [0, 1, 2] and r["picks"][0]["target"] == sc["target"]
    assert len({p["target"] for p in r["picks"]}) == 3, "a failed target is not chosen again"
    assert all(p["outcome"] == "timed_out" and p["score"] == 0.0 for p in r["picks"])
    assert r["score"] == rules.score(r, rules.task(1)) == 0.0 and r["successes"] == 0 and not r["success"]
    rp = harness.replay(sc, r["ctrl"], r["n"], time_limit_s=1)
    assert (rp["outcome"], rp["tick"], [p["target"] for p in rp["picks"]]) == ("done", 60, [p["target"] for p in r["picks"]])
    tr = harness.trace(sc, r["ctrl"], r["n"], time_limit_s=1, result=r)
    assert tr["outcome"] == "done" and len(tr["ticks"]) == 60 and tr["reasons"][0].startswith("done after 3 of 3 picks")
    v = verifier.verify_result(sc, r, time_limit_s=1)
    assert v["ok"] and v["outcome"] == "done" and len(v["picks"]) == 3, v
    s = harness.summary([r])
    assert (s["picks"], s["pick_successes"], s["pick_outcomes"]) == (3, 0, {"timed_out": 3})
    ex = harness.run(EXAMPLE, [sc], time_limit_s=6, wall_s=20)[0]
    assert ex["outcome"] == "done" and len(ex["picks"]) == 3 and ex["error"] is None, ex
    late = harness.run(fake("sleep"), [sc], time_limit_s=15, wall_s=0.6)[0]
    assert late["outcome"] == "wall_timeout" and "in pick 0" in late["error"] and late["wall_s"] >= 0.6, late
    assert late["picks"] and late["picks"][-1]["outcome"] == "wall_timeout" and late["picks"][0]["expert_s"] >= 0.6


def test_the_wall_budget_counts_only_the_experts_time_and_the_safety_cap_is_infra(scene, monkeypatch):
    # a fast expert over 300 ticks: far more elapsed time than its wall budget, all of it simulation
    r = harness.run(fake("hold", "10"), [scene], time_limit_s=15, wall_s=0.3)[0]
    assert r["outcome"] == "timed_out" and r["expert_s"] < 0.3 < r["wall_s"], r
    monkeypatch.setattr(harness, "safety_s", lambda: 0.5)
    r = harness.run(fake("sleep"), [scene], time_limit_s=15, wall_s=30)[0]
    assert (r["outcome"], r["infra"], r["score"]) == ("harness_error", True, None) and "safety cap" in r["error"], r


def test_row_values_beyond_max_abs_end_the_scene_for_every_kind(scene):
    r = harness.run(fake("big"), [scene], time_limit_s=15, wall_s=30)[0]
    assert r["outcome"] == "expert_error" and "outside -100..100" in r["error"], r


def test_multi_pick_score():
    t = rules.task(30)
    picks = [{"outcome": "success", "t": 15.0, "outs": 0}, {"outcome": "missed", "t": 30.0, "outs": 1}] + \
        [{"outcome": "success", "t": 0.0, "outs": 0}] * 8
    r = {"outcome": "done", "picks": picks, "picks_planned": 10, "outs": 1}
    assert rules.score(r, t) == round((0.75 + 8 - 1) / 10, 6)
    assert rules.score({**r, "outs": 0, "picks": [{"outcome": "success", "t": 0.0}] * 10}, t) == 1.0
    assert rules.score({**r, "outs": 2, "picks": picks[:3]}, t) == round((0.75 + 1 - 3) / 10, 6)
    assert rules.score({"outcome": "physics_error", "picks": [], "picks_planned": 10}, t) == 0.0
    assert rules.score({"outcome": "physics_error", "outs": 0, "picks_planned": 10,
                        "picks": [{"outcome": "success", "t": 0.0}, {"outcome": "physics_error", "t": 3.0}]}, t) == 0.1


def _limits(scene, time_limit_s, **kw):
    t = rules.task(time_limit_s)
    return {"time_limit_s": t["time_limit_s"], "picks": rules.scene_picks(scene), "max_rows": t["max_rows"],
            "tick_s": t["tick_s"], "control_hz": t["control_hz"], **kw}


def _claim(r, lim):
    return json.loads(json.dumps(harness.wire_result(r, lim)))


def test_claims_of_real_runs_pass_the_cheap_check(scene):
    sc = _picks_scene(scene)
    runs = [(sc, fake("hold"), 1), (sc, fake("empty"), 1), (sc, EXAMPLE, 6), (scene, EXAMPLE, 15)]
    for s, ex, tl in runs:
        r = harness.run(ex, [s], time_limit_s=tl, wall_s=30)[0]
        lim = _limits(s, tl)
        c = _claim(r, lim)
        assert set(harness.CLAIM_KEYS) <= set(c) and "ctrl_applied" not in c
        assert harness.check_claim(s, c, lim) == [], (r["outcome"], harness.check_claim(s, c, lim))
        assert r["expert_s"] > 0 and r["rows_sent"] == sum(r["act_rows"])
        if r.get("picks"):
            assert all(p["expert_s"] >= 0 for p in r["picks"])
            assert abs(sum(p["expert_s"] for p in r["picks"]) - r["expert_s"]) < 1e-5
    # the empty acts hold: stepped rows without sent rows
    r = harness.run(fake("empty"), [sc], time_limit_s=1, wall_s=30)[0]
    assert (r["act_rows"], r["rows_sent"], r["n"]) == ([0, 0, 0], 0, 60)
    s = harness.summary([r])
    assert s["expert_s"] == round(r["expert_s"], 3) and s["expert_pick_max_s"] == max(p["expert_s"] for p in r["picks"])


def test_check_claim_rejects_what_does_not_add_up(scene):
    sc = _picks_scene(scene)
    r = harness.run(fake("hold", "4"), [sc], time_limit_s=1, wall_s=30)[0]
    lim = _limits(sc, 1)
    good = _claim(r, lim)
    assert harness.check_claim(sc, good, lim) == []

    def bad(change, *, limits=lim, scene_=sc):
        c = copy.deepcopy(good)
        change(c)
        errs = harness.check_claim(scene_, c, limits)
        assert errs, "a tampered claim passed"
        return " | ".join(errs)

    assert "scene_id" in bad(lambda c: c.update(scene_id="other"))
    assert "ctrl" not in good and "act_rows" not in good and "dropped_ctrl" not in good
    assert all(len(p["action_hash"]) == 16 and p["rows_sent"] >= 0 for p in good["picks"])
    assert sum(p["rows_sent"] for p in good["picks"]) == good["rows_sent"]
    assert "action_hash" in bad(lambda c: c.update(action_hash="not a hash"))
    assert "pick 1 action_hash" in bad(lambda c: c["picks"][1].update(action_hash="X" * 16))
    assert "score" in bad(lambda c: c.update(score=1.0))
    assert "pick 1 score" in bad(lambda c: c["picks"][1].update(score=0.5))
    assert "rows_sent" in bad(lambda c: c.update(rows_sent=c["rows_sent"] + 1))
    assert "rows_sent" in bad(lambda c: c["picks"][0].update(rows_sent=lim["max_rows"] + 1))
    assert "tick" in bad(lambda c: c["picks"][0].update(tick=c["picks"][0]["tick"] + 1))
    assert "targets part" in bad(lambda c: c["picks"][2].update(target=c["picks"][1]["target"]))
    assert "pick 0 target" in bad(lambda c: c["picks"][0].update(target=c["picks"][0]["target"] + 1))
    assert "out_parts" in bad(lambda c: c["picks"][1].update(outs=1))
    assert "outs" in bad(lambda c: c.update(outs=2))
    assert "successes" in bad(lambda c: c.update(successes=1))
    assert "picks" in bad(lambda c: None, limits={**lim, "picks": 5})
    assert "not a verdict" in bad(lambda c: c["picks"][0].update(outcome="wall_timeout"))
    assert "picks" in bad(lambda c: c.pop("picks"))
    # another score setting: the claim must state the round's formula
    other = {**lim, "score": {"out_unit": 2.0}}
    assert harness.check_claim(sc, _claim(r, other), other) == []
    assert harness.check_claim(sc, good, {**lim, "score": {"time_weight": 0.1}}) == []  # no success: same score
    # what the cheap check cannot see: a fabricated success with a consistent score (phase 2 re-runs it)
    c = copy.deepcopy(good)
    c["picks"][0]["outcome"] = "success"
    c["successes"] = 1
    c["picks"][0]["score"] = rules.pick_score(c["picks"][0], rules.score_settings(), 1.0)
    c["score"] = rules.score(c, harness.claim_task(lim))
    assert harness.check_claim(sc, c, lim) == []
    # a harness failure before anything ran is a well-formed claim with no rows and no score
    f = harness.wire_result(harness._fail(sc, "docker_unavailable", "no docker", 0.0), lim)
    assert f["score"] is None and harness.check_claim(sc, f, lim) == []


