import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "skills" / "bin-picking" / "example" / "Dockerfile"

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _fake_bin(d: Path, name: str, body: str) -> None:
    p = d / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(0o755)


def _pin(dockerfile: Path) -> str:
    return next(x.split("=", 1)[1].strip() for x in dockerfile.read_text().splitlines()
                if x.startswith("ARG VECTOR_SIM_REF="))


def _repo(tmp_path: Path, skills: dict[str, str | None] | None = None) -> Path:
    """A vector-env checkout holding scripts/setup.sh and one example Dockerfile per skill (None: the real one)."""
    repo = tmp_path / "vector-env"
    (repo / "scripts").mkdir(parents=True)
    (repo / "js").mkdir()
    shutil.copy(ROOT / "scripts" / "setup.sh", repo / "scripts" / "setup.sh")
    for skill, text in ({"bin-picking": None} if skills is None else skills).items():
        ex = repo / "skills" / skill / "example"
        ex.mkdir(parents=True)
        if text is None:
            shutil.copy(DOCKERFILE, ex / "Dockerfile")
        else:
            (ex / "Dockerfile").write_text(text)
    return repo


def _env(tmp_path: Path, **extra: str) -> tuple[dict, Path]:
    """PATH with a fake git (logs, clone makes <dest>/js) and a fake npm (logs its folder)."""
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    _fake_bin(bin_, "git", f'echo "git $@" >> {log}\nif [ "$1" = clone ]; then mkdir -p "${{@: -1}}/js"; exit 0; fi\n'
                          "exit 1\n")
    _fake_bin(bin_, "npm", f'echo "npm $@ in $PWD" >> {log}\nexit 0\n')
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "VECTOR_ENV_JS_ONLY": "1"}
    for k in ("VECTOR_SIM_REF", "VECTOR_SIM_GIT", "VECTOR_ENV_SKILL"):
        env.pop(k, None)
    env.update(extra)
    return env, log


def _setup(repo: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(repo / "scripts" / "setup.sh")], env=env, capture_output=True, text=True,
                          timeout=60)


@needs_bash
def test_setup_clones_vector_sim_at_the_dockerfile_tag(tmp_path):
    repo = _repo(tmp_path)
    pin = _pin(DOCKERFILE)
    assert pin == "bin-picking-expert-r1"
    env, log = _env(tmp_path)
    p = _setup(repo, env)
    calls = log.read_text().splitlines()
    assert p.returncode == 0, p.stderr
    assert calls[0].startswith(f"git clone --depth 1 --branch {pin} ") and calls[0].endswith("../vector-sim")
    assert [c.rsplit(" in ", 1)[1] for c in calls if c.startswith("npm ci")] == [
        str(tmp_path / "vector-sim" / "js"), str(repo / "js"), str(repo / "skills" / "bin-picking" / "example")]
    log.unlink()
    p = _setup(repo, {**env, "VECTOR_SIM_REF": "no-such-tag"})
    assert p.returncode == 0 and "git clone" not in log.read_text()
    shutil.rmtree(tmp_path / "vector-sim")
    p = _setup(repo, {**env, "VECTOR_SIM_REF": "no-such-tag", "VECTOR_SIM_GIT": "https://example.org/sim"})
    assert p.returncode == 0, p.stderr
    assert "--branch no-such-tag https://example.org/sim ../vector-sim" in log.read_text()


@pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None, reason="needs git and bash")
def test_setup_warns_when_vector_sim_is_not_at_the_tag(tmp_path):
    repo = _repo(tmp_path)
    pin = _pin(DOCKERFILE)
    sim = tmp_path / "vector-sim"
    (sim / "js").mkdir(parents=True)
    g = ["git", "-C", str(sim), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(sim)], check=True)
    subprocess.run([*g, "commit", "-q", "--allow-empty", "-m", "a"], check=True)
    subprocess.run([*g, "tag", pin], check=True)
    subprocess.run([*g, "commit", "-q", "--allow-empty", "-m", "b"], check=True)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    _fake_bin(bin_, "npm", "exit 0\n")
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "VECTOR_ENV_JS_ONLY": "1"}
    env.pop("VECTOR_SIM_REF", None)
    env.pop("VECTOR_ENV_SKILL", None)
    p = _setup(repo, env)
    assert p.returncode == 0 and f"not {pin}" in p.stderr and f"checkout {pin}" in p.stderr, p.stderr
    subprocess.run(["git", "-C", str(sim), "checkout", "-q", pin], check=True)
    p = _setup(repo, env)
    assert p.returncode == 0 and "warning" not in p.stderr, p.stderr
    p = _setup(repo, {**env, "VECTOR_SIM_REF": "no-such-tag"})
    assert p.returncode == 0 and "does not have no-such-tag" in p.stderr, p.stderr


def _other(ref: str) -> str:
    return DOCKERFILE.read_text().replace(f"ARG VECTOR_SIM_REF={_pin(DOCKERFILE)}", f"ARG VECTOR_SIM_REF={ref}")


@needs_bash
def test_vector_env_skill_picks_the_skill_folder(tmp_path):
    repo = _repo(tmp_path, {"bin-picking": None, "sorting": _other("sorting-expert-r2")})
    (repo / "skills" / "notes").mkdir()
    env, log = _env(tmp_path, VECTOR_ENV_SKILL="sorting")
    p = _setup(repo, env)
    assert p.returncode == 0, p.stderr
    calls = log.read_text().splitlines()
    assert calls[0].startswith("git clone --depth 1 --branch sorting-expert-r2 ")
    assert calls[-1] == f"npm ci --no-audit --no-fund in {repo / 'skills' / 'sorting' / 'example'}"
    assert not any("bin-picking" in c for c in calls)


@needs_bash
def test_setup_picks_the_only_skill_folder_with_an_example(tmp_path):
    repo = _repo(tmp_path)
    (repo / "skills" / "notes").mkdir()
    (repo / "skills" / "draft" / "example").mkdir(parents=True)
    env, log = _env(tmp_path)
    p = _setup(repo, env)
    assert p.returncode == 0, p.stderr
    assert log.read_text().splitlines()[-1].endswith(f" in {repo / 'skills' / 'bin-picking' / 'example'}")


@needs_bash
def test_several_skill_folders_need_vector_env_skill(tmp_path):
    repo = _repo(tmp_path, {"bin-picking": None, "sorting": _other("sorting-expert-r2")})
    env, log = _env(tmp_path)
    p = _setup(repo, env)
    assert p.returncode == 1 and not log.exists()
    assert len(p.stderr.splitlines()) == 1
    assert "skills/bin-picking" in p.stderr and "skills/sorting" in p.stderr and "VECTOR_ENV_SKILL" in p.stderr


@needs_bash
def test_no_skill_folder_or_an_unknown_one_exits_1(tmp_path):
    repo = _repo(tmp_path, {})
    env, log = _env(tmp_path)
    p = _setup(repo, env)
    assert p.returncode == 1 and not log.exists() and len(p.stderr.splitlines()) == 1
    assert "skills/<skill>/example/Dockerfile" in p.stderr
    shutil.rmtree(repo)
    repo = _repo(tmp_path)
    p = _setup(repo, {**env, "VECTOR_ENV_SKILL": "sorting"})
    assert p.returncode == 1 and not log.exists() and len(p.stderr.splitlines()) == 1
    assert "skills/sorting/example/Dockerfile" in p.stderr and "skills/bin-picking" in p.stderr
