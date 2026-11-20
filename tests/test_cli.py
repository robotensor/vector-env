import json
import shlex
import sys

from conftest import HERE, needs_bundle

from vector_env import cli


@needs_bundle
def test_run_prints_the_score(capsys):
    cmd = shlex.join([sys.executable, str(HERE / "fake_expert.py"), "empty"])
    assert cli.main(["run", "--command", cmd, "--scenes", "2", "--seed", "4", "--time-limit", "3", "--workers", "2"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["scenes"] == 2 and out["score"] == 0.0 and out["time_limit_s"] == 3


@needs_bundle
def test_scene_summary(capsys):
    assert cli.main(["scene", "--seed", "2", "--summary"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["seed"] == 2 and out["parts"] >= 1 and out["env"]["assets_sha256"]
