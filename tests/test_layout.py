import ast
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import vector_env
from vector_env import command, skills
from vector_env.skills import bin_picking

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "vector_env"
MODULES = ("assets", "cli", "compose", "harness", "library", "node", "policy", "rules", "scenes", "settings", "verifier",
           "build")
ALIAS_FILES = {m: PKG / "build" / "__init__.py" if m == "build" else PKG / f"{m}.py" for m in MODULES}
ALIASES = set(ALIAS_FILES.values())
SEVERAL = ("vector-env: several skills are installed (bin-picking, other-skill): give --skill ID or set "
           "VECTOR_ENV_SKILL")


def _run(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    env.pop("VECTOR_ENV_SKILL", None)
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, env=env, timeout=120, cwd=ROOT)


@pytest.mark.parametrize("name", MODULES)
def test_each_old_path_is_the_skill_module(name, monkeypatch):
    old = importlib.import_module(f"vector_env.{name}")
    new = importlib.import_module(f"vector_env.skills.bin_picking.{name}")
    assert old is new
    assert getattr(vector_env, name) is new
    probe = object()
    monkeypatch.setattr(old, "_layout_probe", probe, raising=False)
    assert new._layout_probe is probe


def test_a_patch_through_the_old_name_is_seen_by_the_new_one(monkeypatch):
    from vector_env import scenes
    from vector_env.skills.bin_picking import scenes as new
    marker = lambda *a, **k: "patched"  # noqa: E731
    monkeypatch.setattr(scenes, "generate", marker)
    assert new.generate is marker
    monkeypatch.setattr("vector_env.assets.ROOT", ROOT / "nowhere")
    assert importlib.import_module("vector_env.skills.bin_picking.assets").ROOT == ROOT / "nowhere"


NAMES = {"scenes": "draw_scene", "assets": "find", "cli": "main", "compose": "compose", "harness": "docker_argv",
         "library": "library_dir", "node": "NodeProc", "policy": "request", "rules": "task", "settings": "js_dir",
         "verifier": "pin", "build": "SOURCES"}
THREADED_FIRST_IMPORT = f"""
import threading
barrier = threading.Barrier(6)
errors = []
def first_import():
    barrier.wait()
    for name, attr in {NAMES!r}.items():
        try:
            getattr(__import__("vector_env." + name, fromlist=[attr]), attr)
        except Exception as e:
            errors.append(repr(e))
threads = [threading.Thread(target=first_import) for _ in range(6)]
[t.start() for t in threads]
[t.join() for t in threads]
print(errors)
"""


@pytest.mark.parametrize("name", MODULES)
def test_a_second_object_of_an_old_path_reads_through_to_the_skill_module(name):
    new = importlib.import_module(f"vector_env.{name}")
    path = ALIAS_FILES[name]
    spec = importlib.util.spec_from_file_location(
        f"vector_env.{name}", path, submodule_search_locations=[str(path.parent)] if name == "build" else None)
    second = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(second)
    assert second is not new and sys.modules[f"vector_env.{name}"] is new
    attr = NAMES[name]
    assert getattr(second, attr) is getattr(new, attr)
    with pytest.raises(AttributeError):
        second.no_such_name


def test_old_paths_resolve_on_a_threaded_first_import():
    assert set(NAMES) == set(MODULES)
    for _ in range(3):
        p = _run("-c", THREADED_FIRST_IMPORT)
        assert p.returncode == 0 and p.stdout == "[]\n", p


def test_the_registry_lists_bin_picking(monkeypatch):
    monkeypatch.delenv("VECTOR_ENV_SKILL", raising=False)
    assert skills.available() == {"bin-picking": bin_picking}
    assert skills.get("bin-picking") is bin_picking
    assert bin_picking.ID == "bin-picking"
    with pytest.raises(KeyError) as e:
        skills.get("x")
    assert e.value.args[0] == "unknown skill x (installed: bin-picking)"
    assert skills.default() == "bin-picking"
    monkeypatch.setenv("VECTOR_ENV_SKILL", "other")
    assert skills.default() == "other"


@pytest.fixture
def two_skills(tmp_path, monkeypatch):
    other = tmp_path / "other_skill"
    other.mkdir()
    (other / "__init__.py").write_text('ID = "other-skill"\n')
    (other / "cli.py").write_text("def main(argv=None, prog='vector-env'):\n"
                                  "    print(prog, *(argv or []))\n"
                                  "    return 7\n")
    (tmp_path / "not_a_skill").mkdir()
    (tmp_path / "not_a_skill" / "__init__.py").write_text('ID = "something-else"\n')
    (tmp_path / "helpers.py").write_text("")
    monkeypatch.setattr(skills, "__path__", [*skills.__path__, str(tmp_path)])
    monkeypatch.delenv("VECTOR_ENV_SKILL", raising=False)
    yield
    for name in ("other_skill", "other_skill.cli", "not_a_skill", "helpers"):
        sys.modules.pop(f"vector_env.skills.{name}", None)


def test_a_second_skill_needs_a_choice(two_skills, monkeypatch, capsys):
    assert list(skills.available()) == ["bin-picking", "other-skill"]
    assert skills.default() is None
    assert command.main(["scene", "--seed", "1"]) == 2
    assert capsys.readouterr().err == SEVERAL + "\n"
    assert command.main([]) == 2
    assert capsys.readouterr().err == SEVERAL + "\n"
    assert command.main(["-h"]) == 0
    assert capsys.readouterr().out.startswith("usage: vector-env [--skill ID] <command> [args...]\n")
    assert command.main(["--skill", "other-skill", "go", "--fast"]) == 7
    assert capsys.readouterr().out == "vector-env go --fast\n"
    assert command.main(["--skill=other-skill", "go"]) == 7
    assert capsys.readouterr().out == "vector-env go\n"
    monkeypatch.setenv("VECTOR_ENV_SKILL", "other-skill")
    assert command.main(["go"]) == 7
    assert capsys.readouterr().out == "vector-env go\n"
    assert command.main(["skills"]) == 0
    assert json.loads(capsys.readouterr().out) == {"skills": ["bin-picking", "other-skill"], "default": "other-skill"}


def test_the_command(monkeypatch, capsys):
    monkeypatch.delenv("VECTOR_ENV_SKILL", raising=False)
    assert command.main(["skills"]) == 0
    assert capsys.readouterr().out == '{"skills": ["bin-picking"], "default": "bin-picking"}\n'
    assert command.main(["--skill", "x", "scene", "--seed", "1"]) == 2
    assert capsys.readouterr().err == "vector-env: unknown skill x (installed: bin-picking)\n"
    monkeypatch.setenv("VECTOR_ENV_SKILL", "x")
    assert command.main(["scene", "--seed", "1"]) == 2
    assert capsys.readouterr().err == "vector-env: unknown skill x (installed: bin-picking)\n"
    monkeypatch.delenv("VECTOR_ENV_SKILL")
    with pytest.raises(SystemExit) as e:
        command.main(["--skill", "bin-picking", "--version"])
    assert e.value.code == 0 and capsys.readouterr().out == vector_env.__version__ + "\n"


def test_the_command_without_a_skill(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("VECTOR_ENV_SKILL", raising=False)
    assert command.main(["--skill"]) == 2
    assert capsys.readouterr().err == "vector-env: --skill needs an ID\n"
    monkeypatch.setattr(skills, "__path__", [str(tmp_path)])
    assert skills.available() == {} and skills.default() is None
    assert command.main(["scene", "--seed", "1"]) == 2
    assert capsys.readouterr().err == "vector-env: no skill is installed\n"
    assert command.main(["--help"]) == 0
    assert capsys.readouterr().out.startswith("usage: vector-env [--skill ID] <command> [args...]\n")
    assert command.main(["skills"]) == 0
    assert capsys.readouterr().out == '{"skills": [], "default": null}\n'


def test_the_command_runs_as_a_module():
    p = _run("-m", "vector_env", "--help")
    assert p.returncode == 0 and p.stdout.startswith("usage: vector-env [-h] [--version] {scene,run,replay,assets}"), p
    assert _run("-m", "vector_env.cli", "--help").stdout == p.stdout
    p = _run("-m", "vector_env", "skills")
    assert p.returncode == 0 and p.stdout == '{"skills": ["bin-picking"], "default": "bin-picking"}\n'
    p = _run("-m", "vector_env", "--skill", "x", "scene", "--seed", "1")
    assert p.returncode == 2 and p.stderr == "vector-env: unknown skill x (installed: bin-picking)\n"
    p = _run("-m", "vector_env.build", "--help")
    assert p.returncode == 0 and p.stdout.startswith("usage: python -m vector_env.build"), p
    assert _run("-m", "vector_env.skills.bin_picking.build", "--help").returncode == 0


def test_the_entry_point_is_the_command():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert meta["project"]["scripts"] == {"vector-env": "vector_env.command:main"}


def test_paths(monkeypatch, tmp_path):
    from vector_env import node, settings
    assert settings.HERE == PKG / "skills" / "bin_picking"
    assert settings.ROOT == ROOT
    monkeypatch.delenv("VECTOR_ENV_JS", raising=False)
    assert settings.js_dir() == ROOT / "js"
    monkeypatch.setenv("VECTOR_ENV_JS", str(tmp_path))
    assert settings.js_dir() == tmp_path
    assert node.NodeProc("sim.mjs", tmp_path).script == str(tmp_path / "bin" / "sim.mjs")
    (tmp_path / "skills" / "bin-picking" / "bin").mkdir(parents=True)
    (tmp_path / "skills" / "bin-picking" / "bin" / "sim.mjs").write_text("")
    assert node.NodeProc("sim.mjs", tmp_path).script == str(tmp_path / "skills" / "bin-picking" / "bin" / "sim.mjs")


def test_package_data_and_test_options():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    globs = meta["tool"]["setuptools"]["package-data"]["vector_env"]
    found = {p.relative_to(PKG).as_posix() for g in globs for p in PKG.glob(g)}
    assert {"env.yaml", "skills/bin_picking/env.yaml", "skills/bin_picking/config.yaml",
            "skills/bin_picking/build/materials.yaml"} <= found
    assert meta["tool"]["pytest"]["ini_options"] == {"testpaths": ["tests", "skills"],
                                                     "norecursedirs": ["node_modules", ".*", "*.egg-info"]}


def test_the_old_env_yaml_is_a_link_to_the_skill_one():
    link = PKG / "env.yaml"
    assert link.is_symlink() and os.readlink(link) == "skills/bin_picking/env.yaml"
    assert link.read_bytes() == (PKG / "skills" / "bin_picking" / "env.yaml").read_bytes()


def test_shared_code_names_no_skill():
    shared = [p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts
              and not (p.is_relative_to(PKG / "skills") and p.parent != PKG / "skills")]
    assert PKG / "command.py" in shared and PKG / "skills" / "__init__.py" in shared
    named = [p.relative_to(ROOT).as_posix() for p in shared if p not in ALIASES
             and ("bin_picking" in p.read_text() or "bin-picking" in p.read_text())]
    assert named == []


def _test_files() -> list[Path]:
    return [p for p in ROOT.rglob("test_*.py") if not {"node_modules", ".venv", "__pycache__"} & set(p.parts)]


def test_test_module_names_are_unique_and_import_conftest_at_module_level():
    names = [p.name for p in _test_files()]
    assert len(names) == len(set(names)), sorted(names)
    assert not (ROOT / "tests" / "conftest.py").exists()
    for p in _test_files():
        tree = ast.parse(p.read_text())
        top = set(map(id, tree.body))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in top:
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
                assert "conftest" not in mods, f"{p}: conftest imported at line {node.lineno}"
