import json
import os
import sys
import time
from pathlib import Path

import pytest
from conftest import HERE, ROOT, fake

from vector_env import harness

EXAMPLE = {"command": ["node", str(ROOT / "example" / "solve.mjs")]}
RESULT_KEYS = {"scene_id", "outcome", "success", "t", "ctrl", "n", "wall_s", "error"}


def test_example_expert_over_several_scenes(scenes3):
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
    assert s["scenes"] == 3 and s["successes"] >= 2, s


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
