import asyncio
import threading

import pytest
from conftest import fake
from fake_remote import FakeMiner

from vector_env import harness

T = 5


def remote(miner, scenes, **k):
    return harness.run({"remote": miner}, scenes, time_limit_s=T, wall_s=k.pop("wall_s", 30), **k)


def test_action_hash_definition():
    assert harness.EMPTY_ACTION_HASH == "cbf29ce484222325" == harness.action_hash([])
    assert harness.action_hash([[0, -1.5, 1.5, -1.5, -1.5, -0.0, 1]]) == "6834ebbc8b4f0b58"
    assert harness.action_hash([[1, 2], [3]]) == harness.action_hash([[1.0], [2.0, 3.0]])
    assert harness.action_hash([[0.1]]) != harness.action_hash([[0.1 + 1e-17 + 2e-17]])


def test_multiplexed_scenes_are_deterministic_and_match_a_local_run(scenes3):
    m = FakeMiner(seed=1)
    seen = []
    a = remote(m, scenes3, workers=3, on_result=lambda i, r: seen.append(i))
    assert sorted(seen) == [0, 1, 2]
    order = [k for t, k in m.seen if t == "state"]
    assert len({k for k in order}) == 3 and order != sorted(order), "the scenes did not run interleaved"
    for i, (sc, r) in enumerate(zip(scenes3, a)):
        assert r["scene_id"] == sc["scene_id"] and r["scene_key"] == f"{i}.0" and r["restarts"] == 0, r
        assert r["outcome"] in ("timed_out", "missed", "knocked_out", "slipped") and r["error"] is None, r
        assert r["action_hash"] == harness.action_hash(m.rows[r["scene_key"]]) and r["rows_sent"] == len(m.rows[r["scene_key"]])
        assert m.ends[r["scene_key"]]["outcome"] == r["outcome"]
        assert r["exchange"]["n"] == r["exchanges"] and r["exchange"]["max_ms"] < 1000
        rp = harness.replay(sc, r["ctrl"], r["n"], time_limit_s=T)
        assert (rp["outcome"], rp["t"]) == (r["outcome"], r["t"])
    b = remote(FakeMiner(seed=7, delay_s=(0.0, 0.01)), scenes3, workers=2)
    c = harness.compare(a, b)
    assert c["all_equal"] and c["compared"] == 3 and c["mismatched"] == [] and c["only_a"] == c["only_b"] == [], c
    local = harness.run(fake("track", "5"), scenes3, time_limit_s=T, wall_s=30, workers=2)
    c = harness.compare(a, local)
    assert c["all_equal"], c
    assert [x["ctrl"] for x in a] == [x["ctrl"] for x in local]
    sub = harness.compare(a, local[1:])
    assert sub["compared"] == 2 and sub["all_equal"] and sub["only_a"] == [scenes3[0]["scene_id"]]


def test_a_non_deterministic_expert_is_caught(scenes3):
    a = remote(FakeMiner(noise=True), scenes3[:2], workers=2)
    b = remote(FakeMiner(noise=True), scenes3[:2], workers=2)
    c = harness.compare(a, b)
    assert not c["all_equal"] and c["equal"] == 0 and len(c["mismatched"]) == 2 and c["infra"] == 0, c
    assert all(x["a"] and x["b"] and x["a"] != x["b"] for x in c["scenes"])


def test_step_deadline_pauses_the_simulator(scenes3):
    m = FakeMiner(stall={"1.": 20})
    r = remote(m, scenes3[:2], workers=2, step_deadline_s=0.5)
    assert r[0]["outcome"] in ("timed_out", "missed", "knocked_out", "slipped"), r[0]
    s = r[1]
    assert s["outcome"] == "step_timeout" and not s["infra"] and "step deadline of 0.5 s (tick 20)" in s["error"], s
    assert (s["tick"], s["n"], s["rows_sent"]) == (20, 20, 20) and 0.5 <= s["wall_s"] < 5, s
    assert m.ends["1.0"]["outcome"] == "step_timeout"


def test_wall_cap(scenes3):
    r = remote(FakeMiner(delay_s=(0.15, 0.15), per=1), scenes3[:1], wall_s=5, step_deadline_s=2)[0]
    assert r["outcome"] == "wall_timeout" and "wall budget of 5 s" in r["error"] and 4.9 <= r["wall_s"] < 7, r
    assert 0 < r["n"] < 40 and r["rows_sent"] >= r["n"], r


@pytest.mark.parametrize("kw,match", [({"flood": True}, "more than 4 messages without waiting"),
                                      ({"error": True}, "cannot plan")])
def test_bad_remote_experts(scenes3, kw, match):
    r = remote(FakeMiner(**kw), scenes3[:1])[0]
    assert r["outcome"] == "expert_error" and match in r["error"] and not r["infra"], r


def test_stray_messages_are_dropped(scenes3):
    r = remote(FakeMiner(junk=True), scenes3[:1])[0]
    assert r["outcome"] != "expert_error" and r["error"] is None, r


def test_disconnect_is_the_miners_failure(scenes3):
    m = FakeMiner(close_after=5)
    r = remote(m, scenes3, workers=2)
    assert [x["outcome"] for x in r] == ["disconnected"] * 3 and not any(x["infra"] for x in r), r
    assert "socket dropped" in r[0]["error"] and all(x["action_hash"] for x in r)


def test_channel_bug_is_infrastructure(scenes3):
    r = remote(FakeMiner(broken_after=3), scenes3[:2], workers=2)
    assert [x["outcome"] for x in r] == ["harness_error"] * 2 and all(x["infra"] for x in r), r
    assert "KeyError" in r[0]["error"]


def test_reconnect_restarts_scenes_in_flight(scenes3):
    clean = remote(FakeMiner(), scenes3[:2], workers=2)
    m = FakeMiner(reconnect_after=6)
    r = remote(m, scenes3[:2], workers=2)
    assert [x["restarts"] for x in r] == [1, 1] and [x["scene_key"] for x in r] == ["0.1", "1.1"], r
    assert harness.compare(clean, r)["all_equal"]
    r = remote(FakeMiner(reconnect_after=6), scenes3[:1], max_restarts=0)[0]
    assert r["outcome"] == "disconnected" and "no restarts left" in r["error"], r


def test_a_drop_longer_than_the_step_deadline_restarts_instead_of_timing_out(scenes3):
    clean = remote(FakeMiner(), scenes3[:2], workers=2)
    m = FakeMiner(drop=(6, 1.2))
    r = remote(m, scenes3[:2], workers=2, step_deadline_s=0.3)
    assert m.drops == 1
    assert [x["restarts"] for x in r] == [1, 1] and [x["scene_key"] for x in r] == ["0.1", "1.1"], r
    assert not any(x["outcome"] in ("step_timeout", "wall_timeout", "disconnected") for x in r), r
    assert harness.compare(clean, r)["all_equal"]


def test_the_first_act_has_its_own_deadline(scenes3):
    r = remote(FakeMiner(first_delay_s=0.6), scenes3[:1], step_deadline_s=0.3, first_step_deadline_s=2)[0]
    assert r["outcome"] != "step_timeout" and r["error"] is None, r
    r = remote(FakeMiner(stall={"0.": 0}), scenes3[:1], step_deadline_s=0.3, first_step_deadline_s=0.5)[0]
    assert r["outcome"] == "step_timeout" and "first-step deadline of 0.5 s (tick 0)" in r["error"], r
    assert 0.5 <= r["wall_s"] < 5, r


def test_async_and_threaded_entry_points(scenes3):
    async def main():
        with pytest.raises(RuntimeError, match="await harness.arun"):
            harness.run({"remote": FakeMiner()}, scenes3[:1], time_limit_s=1, wall_s=10)
        got = []

        async def on_result(i, r):
            got.append(i)

        r = await harness.arun({"remote": FakeMiner()}, scenes3[:1], time_limit_s=1, wall_s=10, on_result=on_result)
        loop = asyncio.get_running_loop()
        out = {}
        t = threading.Thread(target=lambda: out.update(r=harness.run({"remote": FakeMiner(), "loop": loop}, scenes3[:1],
                                                                       time_limit_s=1, wall_s=10)))
        t.start()
        while t.is_alive():
            await asyncio.sleep(0.05)
        local = await harness.arun(fake("track", "5"), scenes3[:1], time_limit_s=1, wall_s=10)
        return r, out["r"], got, local

    a, b, got, local = asyncio.run(main())
    assert got == [0] and a[0]["n"] == 20 and harness.compare(a, b)["all_equal"] and harness.compare(a, local)["all_equal"]


def test_compare_pairs_by_position_without_ids():
    a = [{"action_hash": "1"}, {"action_hash": "2"}]
    c = harness.compare(a, [{"action_hash": "1"}, {"action_hash": None, "outcome": "harness_error"}])
    assert (c["compared"], c["equal"], c["infra"], c["all_equal"]) == (2, 1, 1, False)
    with pytest.raises(ValueError):
        harness.compare(a, a[:1])
