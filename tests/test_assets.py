import json
import shutil
from pathlib import Path

import pytest

from vector_env import assets


def tiny(root: Path) -> Path:
    b = root / "src"
    (b / "robot").mkdir(parents=True)
    (b / "robot" / "a.stl").write_bytes(b"solid a")
    (b / "library").mkdir()
    (b / "library" / "index.jsonl").write_text("{}\n")
    (b / "manifest.json").write_text(json.dumps({"bundle": "abc", "files": []}))
    return b


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("VECTOR_ENV_BUNDLE", raising=False)
    monkeypatch.delenv("VECTOR_SIM_BUNDLE", raising=False)
    monkeypatch.setattr(assets, "ROOT", tmp_path / "nowhere")
    monkeypatch.setenv("VECTOR_ENV_CACHE", str(tmp_path / "cache"))
    return tmp_path


def test_pack_pull_verify(env, monkeypatch):
    src = tiny(env)
    m = assets.write_manifest(src, "t1")
    assert m["files"] == 3 and assets.info(src) == {"version": "t1", "sha256": m["sha256"]}
    p = assets.pack(src, env / "pub")
    assert (env / "pub" / "t1" / assets.ARCHIVE).is_file() and p["sha256"] == m["sha256"]
    monkeypatch.setenv("VECTOR_ENV_ASSETS_SOURCE", str(env / "pub"))
    monkeypatch.setenv("VECTOR_ENV_ASSETS_VERSION", "t1")
    monkeypatch.setenv("VECTOR_ENV_ASSETS_SHA256", m["sha256"])
    d = assets.ensure()
    assert d == env / "cache" / "t1" and (d / "robot" / "a.stl").read_bytes() == b"solid a"
    assert assets.info() == {"version": "t1", "sha256": m["sha256"]} and assets.verify(d) == []
    assert assets.ensure() == d and assets.find() == d
    (d / "robot" / "a.stl").write_bytes(b"changed")
    assert assets.verify(d) == ["robot/a.stl"]
    monkeypatch.setenv("VECTOR_ENV_ASSETS_SHA256", "0" * 64)
    shutil.rmtree(d)
    with pytest.raises(assets.AssetsError, match="not the pinned"):
        assets.ensure()


def test_tampered_archive_is_refused(env, monkeypatch):
    src = tiny(env)
    assets.write_manifest(src, "t2")
    assets.pack(src, env / "pub")
    (src / "robot" / "a.stl").write_bytes(b"other")
    shutil.copyfile(src / assets.MANIFEST, env / "keep.json")
    assets.pack(src, env / "pub2", "t2")
    shutil.copyfile(env / "pub2" / "t2" / assets.ARCHIVE, env / "pub" / "t2" / assets.ARCHIVE)
    with pytest.raises(assets.AssetsError, match="missing or changed"):
        assets.pull("t2", str(env / "pub"), env / "out")
    assert not (env / "out").exists()


def test_hugging_face_source(env, monkeypatch):
    src = tiny(env)
    assets.write_manifest(src, "t3")
    assets.pack(src, env / "pub")
    calls = []

    def fake_download(repo_id, filename, repo_type, revision, token, local_dir):
        calls.append((repo_id, filename, repo_type, revision))
        out = Path(local_dir) / filename
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(env / "pub" / filename, out)
        return str(out)

    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)
    d = assets.pull("t3", "hf://team/assets@v1", env / "hf")
    assert (d / "robot" / "a.stl").is_file()
    assert calls == [("team/assets", "t3/assets.json", "dataset", "v1"), ("team/assets", "t3/bundle.tar", "dataset", "v1")]


def test_no_source_is_an_error(env, monkeypatch):
    monkeypatch.setenv("VECTOR_ENV_ASSETS_VERSION", "nope")
    monkeypatch.delenv("VECTOR_ENV_ASSETS_SOURCE", raising=False)
    monkeypatch.setattr(assets, "env_config", lambda: {"assets": {"version": "nope", "sha256": None, "source": None}})
    with pytest.raises(assets.AssetsError, match="no asset source"):
        assets.ensure()


def test_cli_without_a_source_prints_one_line(env, monkeypatch, capsys):
    from vector_env import cli
    monkeypatch.delenv("VECTOR_ENV_ASSETS_SOURCE", raising=False)
    monkeypatch.setattr(assets, "pinned", lambda: {"version": "t9", "sha256": None, "source": None})
    assert cli.main(["assets", "pull"]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "no asset source" in err[0]
    assert cli.main(["assets", "dir"]) == 2
    assert "no asset bundle" in capsys.readouterr().err
    assert assets.find() is None


def test_a_bundle_folder_is_not_a_source(env):
    src = tiny(env)
    with pytest.raises(assets.AssetsError, match="is a bundle folder"):
        assets.pull("t1", str(src), env / "out")


@pytest.mark.parametrize("v", ["../..", "..", "a/../b", "a/b", "/abs", ".hidden", "", "a b", "x" * 80])
def test_bad_versions_are_refused(env, monkeypatch, v):
    monkeypatch.setenv("VECTOR_ENV_ASSETS_VERSION", v)
    monkeypatch.setenv("VECTOR_ENV_ASSETS_SOURCE", str(env / "pub"))
    victim = env / "victim"
    victim.mkdir()
    if v:
        with pytest.raises(assets.AssetsError, match="bad asset version"):
            assets.ensure()
    with pytest.raises(assets.AssetsError, match="bad asset version"):
        assets.pull(v, str(env / "pub"))
    assert victim.is_dir()


def _republish(env, version, edit):
    man_p = env / "pub" / version / assets.MANIFEST
    man = json.loads(man_p.read_text())
    edit(man)
    man_p.write_text(json.dumps(man))


def test_manifest_paths_and_unlisted_archive_members_are_refused(env, monkeypatch, caplog):
    import tarfile
    src = tiny(env)
    assets.write_manifest(src, "t4")
    assets.pack(src, env / "pub")
    with caplog.at_level("WARNING"):
        d = assets.pull("t4", str(env / "pub"), env / "ok")
    assert assets.verify(d) == [] and "no pinned sha256" in caplog.text
    (d / "robot" / "extra.stl").write_bytes(b"x")
    assert assets.verify(d) == ["robot/extra.stl (not in the manifest)"]
    for bad in ("../escape.stl", "/etc/passwd", "robot/../a.stl", "robot//a.stl", "C:/x.stl", "assets.json"):
        _republish(env, "t4", lambda m: m["files"].append([bad, "0" * 64, 1]))
        with pytest.raises(assets.AssetsError, match="bad asset path"):
            assets.pull("t4", str(env / "pub"), env / "out")
        _republish(env, "t4", lambda m: m["files"].pop())
    tar_p = env / "pub" / "t4" / assets.ARCHIVE
    with tarfile.open(tar_p, "a") as tf:
        (env / "junk").write_bytes(b"junk")
        tf.add(env / "junk", arcname="robot/junk.stl")
    with pytest.raises(assets.AssetsError, match="does not list"):
        assets.pull("t4", str(env / "pub"), env / "out")
    assert not (env / "out").exists()
    man = assets.read_manifest(d)
    man["files"].append(["../x", "0" * 64, 1])
    (d / assets.MANIFEST).write_text(json.dumps(man))
    assert any("bad entry" in b for b in assets.verify(d))
