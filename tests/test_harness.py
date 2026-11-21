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
