import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from vector_env import harness
from vector_env import policy as P
from vector_env.rules import OUTCOMES


class FakePolicy:
    def __init__(self, mode: str = "hold", per: int = 5):
        self.mode, self.per, self.seen = mode, per, []
        owner = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                owner.seen.append({"auth": self.headers.get("authorization"), "path": self.path, **body})
                code, out = owner.answer(body)
                raw = out if isinstance(out, bytes) else json.dumps(out).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def answer(self, body):
        if self.mode == "503":
            return 503, {"error": "warming up"}
        if self.mode == "400":
            return 400, {"error": "bad request"}
        if self.mode == "slow":
            time.sleep(1.5)
        R = body["meta"]["robot"]
        q = [body["qpos"][i] for i in R["arm_joints_qpos"]]
        if self.mode == "nan":
            return 200, b'{"model": "m", "ctrl": [[NaN, 0, 0, 0, 0, 0, 0]]}'
        row = [x + 0.001 * body["call_in_try"] for x in q] + [0.0]
        return 200, {"model": "fake-policy-1", "ctrl": [row] * self.per, "state": {"k": body["call_in_try"]},
                     "give_up": self.mode == "give_up"}

    def spec(self, **k):
        return {"policy": {"url": self.url, "token": "tk", **k}}

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def fake_policy():
    made = []

    def make(*a, **k):
        made.append(FakePolicy(*a, **k))
        return made[-1]

    yield make
    for p in made:
        p.close()


def test_the_policy_view_has_no_scene_state():
    start = {"scene": {"scene_id": "s1", "xml": "<mujoco/>",
                       "meta": {"seed": 7, "fill": {"spawns": [1]}, "parts": [1], "camera": {"w": 1}, "tick_s": 0.05,
                                "robot": {"arm_joints_qpos": [0, 1, 2, 3, 4, 5], "driver_qpos": 6}}},
             "checkpoint": "c", "state_hash": "h", "target": 2}
    msg = {"qpos": list(range(49)), "obs": {"depth": "d"}}
    v = P.request(start, msg, 3, {"a": 1}, "run-1")
    assert v == {"episode": "run-1:s1", "call": "c3", "call_in_try": 3, "target": 2, "obs": {"depth": "d"},
                 "qpos": [0, 1, 2, 3, 4, 5, 6], "state": {"a": 1},
                 "meta": {"camera": {"w": 1}, "robot": start["scene"]["meta"]["robot"], "tick_s": 0.05}}


def test_a_policy_service_runs_scenes_through_the_harness(scenes3, fake_policy):
    pol = fake_policy()
    res = harness.run(pol.spec(calls=4), scenes3[:2], time_limit_s=3, wall_s=60, workers=2)
    assert [r["scene_id"] for r in res] == [sc["scene_id"] for sc in scenes3[:2]]
    for r in res:
        assert r["outcome"] in OUTCOMES and r["model"] == "fake-policy-1" and not r["infra"]
        assert r["rows_sent"] == 4 * 5 and r["n"] == 60
    calls = [c for c in pol.seen if c["episode"].endswith(scenes3[0]["scene_id"])]
    assert [c["call_in_try"] for c in calls] == [0, 1, 2, 3]
    assert [c["state"] for c in calls] == [None, {"k": 0}, {"k": 1}, {"k": 2}]
    for c in pol.seen:
        assert c["auth"] == "Bearer tk" and c["path"] == "/v1/act"
        assert set(c["meta"]) <= set(P.META_KEYS) and "robot" in c["meta"]
        assert len(c["qpos"]) == 7 and c["obs"].get("depth")
        assert not {"xml", "checkpoint", "scene", "seed", "fill"} & set(c)
    again = harness.run(pol.spec(calls=4), scenes3[:2], time_limit_s=3, wall_s=60)
    assert [r["action_hash"] for r in again] == [r["action_hash"] for r in res]


def test_a_policy_that_gives_up_holds_until_the_verdict(scene, fake_policy):
    pol = fake_policy("give_up")
    r = harness.run(pol.spec(), [scene], time_limit_s=2, wall_s=60)[0]
    assert r["outcome"] in OUTCOMES and r["rows_sent"] == 0 and len(pol.seen) == 1


@pytest.mark.parametrize("mode,outcome,infra", [("503", "policy_unavailable", True), ("400", "expert_error", False),
                                                ("nan", "expert_error", False), ("slow", "policy_unavailable", True)])
def test_policy_failures_end_the_scene(scene, fake_policy, mode, outcome, infra):
    pol = fake_policy(mode)
    r = harness.run(pol.spec(timeout_s=0.5), [scene], time_limit_s=2, wall_s=60)[0]
    assert (r["outcome"], r["infra"]) == (outcome, infra), r["error"]


def test_a_policy_that_is_down_is_infrastructure(scene):
    r = harness.run({"policy": {"url": "http://127.0.0.1:9", "timeout_s": 2}}, [scene], time_limit_s=2, wall_s=60)[0]
    assert r["outcome"] == "policy_unavailable" and r["infra"] and "no answer" in r["error"]
