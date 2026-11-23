import base64
import struct

import pytest

from conftest import ROOT, needs_bundle
from vector_env import harness, verifier

EXAMPLE = {"command": ["node", str(ROOT / "example" / "solve.mjs")]}
ROWS = [[0.5, -1.25, 3.0, -0.0, 1e-300, 2.5e10, 1.0], [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.0]]


def test_hashes_match_the_published_vectors_and_the_harness():
    assert verifier.rows_hash(ROWS) == harness.action_hash(ROWS) == "8110603038cb9e9a"
    assert verifier.rows_hash([]) == "cbf29ce484222325"
    seed = verifier.chain_seed("n0nce", "s1", "scene-a")
    assert seed == "a84b0246363373a6"
    assert verifier.chain_next(seed, 0, 0, 2, "0123456789abcdef", ROWS) == "84e03fe56edce983"
    b64 = base64.b64encode(struct.pack("<14d", *[x for r in ROWS for x in r])).decode()
    assert verifier.chain_next(seed, 0, 0, 2, "0123456789abcdef", b64) == "84e03fe56edce983"
    assert verifier.rows_hash(b64) == "8110603038cb9e9a"


def test_bad_rows_are_refused():
    with pytest.raises(verifier.VerifyError):
        verifier.row_bytes(b"\0" * 8)
    with pytest.raises(verifier.VerifyError):
        verifier.row_bytes([[0, 0, 0, 0, 0, 0, float("nan")]])


@pytest.fixture(scope="module")
def solved(scenes3):
    res = harness.run(EXAMPLE, scenes3, time_limit_s=15, wall_s=60)
    for sc, r in zip(scenes3, res):
        if r["outcome"] == "success":
            return sc, r
    pytest.skip("the example expert solved none of the scenes")


@needs_bundle
def test_an_expert_result_and_its_human_cut_verify(solved):
    sc, r = solved
    v = verifier.verify_result(sc, r, time_limit_s=15)
    assert v["ok"] and v["success"], v
    assert v["action_hash"] == verifier.rows_hash(r["ctrl"])
    if r["rows_sent"] == r["n"]:
        assert v["action_hash"] == r["action_hash"]
    raw = base64.b64decode(r["ctrl"])[: v["tick"] * 56]
    h = verifier.verify(sc, raw, time_limit_s=30, claim={"outcome": "success", "tick": v["tick"], "state_hash": v["state_hash"]})
    assert h["ok"] and h["tick"] == v["tick"] and h["state_hash"] == v["state_hash"], h
    forged = verifier.verify(sc, raw[: 56 * 10], time_limit_s=30, claim={"outcome": "success"})
    assert not forged["ok"] and forged["reason"] in ("no_verdict", "outcome_mismatch")
    wrong = verifier.verify(sc, raw, time_limit_s=30, claim={"state_hash": "0" * 16})
    assert wrong["reason"] == "state_mismatch"
    moved = verifier.verify({**sc, "state_hash": "0" * 16}, raw, time_limit_s=30)
    assert moved["reason"] == "scene_error"


@needs_bundle
def test_the_probe_is_stable(scene):
    a, b = verifier.probe(scene), verifier.probe(scene)
    assert a == b and a["start"] == scene["state_hash"] and len(a["marks"]) == 8
    p = verifier.pin()
    assert len(p["code"]) == 64 and len(p["wasm"]) == 64 and p["mujoco"]


@needs_bundle
def test_a_console_claim_refuses_rows_outside_the_actuator_ranges_and_results_carry_the_applied_rows(solved):
    sc, r = solved
    v = verifier.verify_result(sc, r, time_limit_s=15)
    raw = base64.b64decode(r["ctrl"])[: v["tick"] * 56]
    claim = {"outcome": "success", "tick": v["tick"], "in_range": True}
    assert verifier.verify(sc, raw, time_limit_s=30, claim=claim)["ok"]
    vals = list(struct.unpack(f"<{len(raw) // 8}d", raw))
    poison = struct.unpack("<d", struct.pack("<Q", 0x4130000000000000 | 0x4E4F53494F50))[0]
    for col, x in ((6, -1e6), (6, poison), (1, 50.0)):
        bad = vals[:]
        bad[7 * 3 + col] = x
        out = verifier.verify(sc, struct.pack(f"<{len(bad)}d", *bad), time_limit_s=30, claim=claim)
        assert not out["ok"] and out["reason"] == "out_of_range", out
    ap = struct.unpack(f"<{r['n'] * 7}d", base64.b64decode(r["ctrl_applied"]))
    lim = sc["meta"]["robot"]["joint_range"]
    for k, x in enumerate(ap):
        lo, hi = (0.0, 1.0) if k % 7 == 6 else lim[k % 7]
        assert lo <= x <= hi
