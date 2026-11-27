"""Another asset bundle in the same format, chosen by the caller, next to the env.yaml one in one process."""
import json
import shutil
import sys
import threading
from pathlib import Path

import pytest
from conftest import HERE, fake, needs_bundle

from vector_env import assets, cli, harness, library, node, scenes, verifier

CLASSIC = {"mode": "classic"}  # these tests pin the old scattered draw; the pile draw has its own tests

pytestmark = needs_bundle
TAG = "alt"


def _make(src: Path, out: Path, ids: list[str], version: str) -> Path:
    """A small bundle: the cell (robot, crates, demo meshes) of src and copies of a few library assets under new ids."""
    out.mkdir(parents=True)
    for f in ("manifest.json", "library.json"):
        shutil.copyfile(src / f, out / f)
    for d in ("robot", "crates", "parts"):
        shutil.copytree(src / d, out / d)
    for aid in ids:
        row = library.get(src, aid)
        rel = Path(row["files"]["ray"]).parent
        unit = rel.name
        dest = out / "assets" / TAG / unit
        shutil.copytree(src / rel, dest)
        doc = json.loads((dest / "asset.json").read_text())
        doc["rows"] = [r for r in doc["rows"] if r["id"] == aid]
        text = json.dumps(doc).replace(f"{rel.as_posix()}/", f"assets/{TAG}/{unit}/").replace(f"lib-{unit}", f"{TAG}-{unit}")
        (dest / "asset.json").write_text(text)
    library.write_index(out, log=lambda *_: None)
    assets.write_manifest(out, version)
    return out


@pytest.fixture(scope="session")
def other(tmp_path_factory):
    src = assets.ensure()
    pool = [i for i in scenes._ids(str(src), 60, 150, 25, 0.03, 0.8) if i.startswith("lib-")]
    ids = [pool[k * len(pool) // 5] for k in range(5)]
    b = _make(src, tmp_path_factory.mktemp("other") / "b", ids, "test.1")
    return b, {f"{TAG}-{i[4:]}" for i in ids}, ids


def _types(sc):
    return {p["type"] for p in sc["meta"]["parts"]}


def test_scenes_use_only_its_assets_and_name_it(other):
    b, ids, _ = other
    sc = scenes.generate(7, CLASSIC, bundle=b)
    assert _types(sc) <= ids
    assert all(f.startswith(f"assets/{TAG}/") for f in sc["meta"]["files"] if f.startswith("assets/"))
    assert sc["env"]["assets_version"] == "test.1" and sc["env"]["assets_sha256"] == assets.info(b)["sha256"]
    assert scenes.verify(sc, bundle=b)
    base = scenes.generate(7, CLASSIC)
    assert not _types(base) & ids and base["state_hash"] != sc["state_hash"]
    assert base["env"]["assets_sha256"] == assets.info(assets.ensure())["sha256"]
    assert assets.for_scene(sc, b) == b and assets.for_scene(base, b) == assets.ensure()
    with pytest.raises(Exception):
        harness.replay(sc, "", 0, time_limit_s=5)


def test_draws_settings_and_caches_stay_apart(other, tmp_path):
    b, ids, _ = other
    assert set(scenes.settings_for(3, CLASSIC, bundle=b)["parts"]) <= ids
    assert not set(scenes.settings_for(3, CLASSIC)["parts"]) & ids
    sc, err = scenes.draw_index(11, 2, CLASSIC, bundle=b)
    assert err is None and _types(sc) <= ids
    c0 = scenes.SceneCache(tmp_path, scenes.env_info())
    c1 = scenes.SceneCache(tmp_path, scenes.env_info(b))
    assert c0.dir != c1.dir and not c0.put(11, 2, sc, None) and c1.put(11, 2, sc, None)
    assert c1.get(11, 2)[0]["state_hash"] == sc["state_hash"] and c0.get(11, 2) is None
    assert cli.scene_cache(tmp_path, None, b).dir == c1.dir
    assert node.sim(b) is node.sim(str(b)) and node.sim(b) is not node.sim(assets.ensure())


def test_both_bundles_in_parallel_threads_give_the_sequential_scenes(other):
    b, _, _ = other
    want = {(k, s): scenes.generate(s, CLASSIC, bundle=k)["state_hash"] for k in (None, b) for s in (21, 22)}
    got, errs = {}, []

    def one(k, s):
        try:
            got[(k, s)] = scenes.generate(s, CLASSIC, bundle=k)["state_hash"]
        except Exception as e:
            errs.append(e)
        finally:
            node.close_thread_sims()
    ts = [threading.Thread(target=one, args=key) for key in want]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs and got == want


def test_run_replay_trace_and_verify_on_it(other):
    b, _, _ = other
    sc = scenes.generate(5, CLASSIC, bundle=b)
    r = harness.run(fake("hold", "20"), [sc], time_limit_s=5, wall_s=30, bundle=b)[0]
    assert r["outcome"] == "timed_out" and r["n"] == 100, r
    rp = harness.replay(sc, r["ctrl"], r["n"], time_limit_s=5, result=r, bundle=b)
    assert (rp["outcome"], rp["t"]) == (r["outcome"], r["t"])
    tr = harness.trace(sc, r["ctrl"], r["n"], time_limit_s=5, result=r, bundle=b)
    assert tr["outcome"] == r["outcome"]
    v = verifier.verify_result(sc, r, time_limit_s=5, bundle=b)
    assert v["ok"] and v["outcome"] == r["outcome"], v
    assert verifier.workers(b) is verifier.workers(str(b)) and verifier.workers(b) is not verifier.workers()
    try:
        assert verifier.verify_result(sc, r, time_limit_s=5)["ok"] is False
    except node.SimError:
        pass


def test_docker_mounts_the_bundle_of_the_call(other, tmp_path):
    b, _, _ = other
    sc = scenes.generate(5, CLASSIC, bundle=b)
    log = tmp_path / "argv.txt"
    shim = tmp_path / "docker"
    shim.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\nexec {sys.executable} {HERE / 'fake_docker.py'} \"$@\"\n")
    shim.chmod(0o755)
    r = harness.run({"image": "fake:hold-20"}, [sc], time_limit_s=5, wall_s=30, docker={"binary": str(shim)}, bundle=b)[0]
    assert (r["outcome"], r["n"]) == ("timed_out", 100), r
    runs = [x for x in log.read_text().splitlines() if x.startswith("run ")]
    assert len(runs) == 1 and f"-v {b}:/bundle:ro" in runs[0] and "VECTOR_SIM_BUNDLE=/bundle" in runs[0]
    argv, _ = harness.docker_argv("i", "n", None, assets.ensure())
    assert argv[argv.index("-v") + 1] == f"{assets.ensure()}:/bundle:ro"


def test_a_pin_is_pulled_once_and_a_wrong_sha_is_refused(other, tmp_path, monkeypatch):
    b, _, _ = other
    monkeypatch.setenv("VECTOR_ENV_CACHE", str(tmp_path / "cache"))
    pub = tmp_path / "pub"
    sha = assets.pack(b, pub)["sha256"]
    with pytest.raises(assets.AssetsError, match="not the pinned") as e:
        assets.resolve({"version": "test.1", "sha256": "0" * 64, "source": str(pub)})
    assert str(pub) not in str(e.value)
    with pytest.raises(assets.AssetsError, match="no source"):
        assets.resolve({"version": "test.1", "sha256": sha})
    with pytest.raises(assets.AssetsError, match="not the pinned"):
        assets.resolve({"version": "test.1", "sha256": "1" * 64, "source": str(b)})
    pin = {"version": "test.1", "sha256": sha, "source": str(pub)}
    d = assets.resolve(pin)
    assert d == assets.pin_dir("test.1", sha) == tmp_path / "cache" / f"test.1~{sha[:16]}"
    assert assets.info(d) == {"version": "test.1", "sha256": sha} and assets.verify(d) == []
    assert assets.resolve({"version": "test.1", "sha256": sha}) == d
    assert assets.resolve({"version": "test.1", "sha256": sha, "source": str(b)}) == d
    sc = scenes.generate(5, CLASSIC, bundle=pin)
    assert sc["env"]["assets_sha256"] == sha and sc["state_hash"] == scenes.generate(5, CLASSIC, bundle=b)["state_hash"]


def test_the_cli_takes_a_bundle(other, capsys, tmp_path):
    b, ids, _ = other
    assert cli.main(["scene", "--draw", '{"mode": "classic"}', "--seed", "5", "--summary", "--bundle", str(b)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["env"]["assets_version"] == "test.1"
    sha = assets.info(b)["sha256"]
    assert cli.main(["assets", "info", "--bundle", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["sha256"] == sha
    sc = scenes.generate(5, CLASSIC, bundle=b)
    r = harness.run(fake("hold", "20"), [sc], time_limit_s=5, wall_s=30, bundle=b)[0]
    (tmp_path / "s.json").write_text(json.dumps(sc))
    (tmp_path / "r.json").write_text(json.dumps(r))
    assert cli.main(["replay", "--scene", str(tmp_path / "s.json"), "--result", str(tmp_path / "r.json"),
                     "--time-limit", "5", "--bundle", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == r["outcome"]
    assert cli.main(["run", "--draw", '{"mode": "classic"}', "--command", " ".join(fake("hold", "20")["command"]), "--scenes", "2", "--time-limit", "5",
                     "--bundle", str(b), "--no-scene-cache"]) == 0
    assert json.loads(capsys.readouterr().out)["scenes"] == 2
    assert cli.main(["scene", "--draw", '{"mode": "classic"}', "--seed", "5", "--bundle", str(b), "--assets-version", "x"]) == 2


def test_a_library_smaller_than_the_kinds_still_draws(other, tmp_path):
    _, _, src_ids = other
    two = _make(assets.ensure(), tmp_path / "two", src_ids[:2], "test.2")
    for seed in range(30):
        assert len(scenes.settings_for(seed, {"mode": "classic", "kinds": [3, 4]}, bundle=two)["parts"]) <= 2
    sc, err = scenes.draw_index(1, 0, {"mode": "classic", "kinds": [3, 4]}, bundle=two)
    assert err is None and len(_types(sc)) <= 2
