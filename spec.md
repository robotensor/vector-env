# vector-env spec

vector-env holds the task environment of each Vector skill: scene generator, asset bundle, task rules and the
expert step harness. Python package `vector_env`, npm package `@vector/env`. This file covers the repository and
the shared parts; each skill has its own spec (`skills/<skill>/spec.md`).

## 1. Layout

| path | what |
|---|---|
| `vector_env/__init__.py` | `__version__` (unchanged) |
| `vector_env/__main__.py` | `python -m vector_env` = the `vector-env` command |
| `vector_env/command.py` | the `vector-env` command (section 3) |
| `vector_env/skills/__init__.py` | the skill registry (section 2) |
| `vector_env/skills/<skill_py>/` | one skill's Python code and data (`bin_picking`) |
| old module paths in `vector_env/` | section 4 |
| `js/package.json`, `js/package-lock.json` | `@vector/env` (package root unchanged) |
| `js/skills/<skill>/{src,bin,test}/` | one skill's Node code |
| `js/test/` | tests of the npm package itself |
| `skills/<skill>/` | one skill's `README.md`, `spec.md`, `example/` and `tests/` |
| `tests/` | shared tests (registry, command, old paths, `setup.sh`) |
| `scripts/setup.sh`, `pyproject.toml`, `requirements.lock`, `README.md`, `spec.md` | the repository |

Skills: `bin-picking` (`vector_env/skills/bin_picking/`, `js/skills/bin-picking/`, `skills/bin-picking/`).

Shared code names no skill. The only shared files that name one are the old paths of section 4, the
compatibility entries of `js/package.json` (section 5) and the tests of those.

## 2. Skill registry

```python
vector_env.skills.available() -> dict[str, types.ModuleType]   # {skill id: skill package}, sorted by id
vector_env.skills.get(skill_id: str) -> types.ModuleType        # KeyError("unknown skill X (installed: a, b)")
vector_env.skills.default() -> str | None                       # $VECTOR_ENV_SKILL, else the only skill, else None
```

- A skill is a subpackage `vector_env.skills.<skill_py>` whose `__init__` defines `ID` (kebab-case) with
  `ID.replace("-", "_") == <skill_py>`. Other subpackages are not skills.
- A skill package's `__init__` imports nothing else (no numpy, mujoco or Node).
- Each skill has a module `cli` with `main(argv: list[str] | None = None, prog: str = "vector-env") -> int`.
- Adding a skill is adding its folders; shared code does not change.

## 3. The `vector-env` command

Entry point: `vector-env = "vector_env.command:main"`; `python -m vector_env` runs the same.

```
vector-env [--skill ID] <command> [args...]
vector-env skills
```

- `--skill ID` or `--skill=ID` is read only as the first argument, before the command. Without it the skill is
  `vector_env.skills.default()`. `--skill` with no ID exits 2 with `vector-env: --skill needs an ID`.
- `skills` prints one JSON line `{"skills": [<ids>], "default": <id or null>}` and exits 0.
- An unknown skill exits 2 with `vector-env: unknown skill X (installed: a, b)`.
- No skill chosen while several are installed: `-h` or `--help` prints the command's own usage
  (`usage: vector-env [--skill ID] <command> [args...]` / `vector-env skills`) and exits 0; any other command line
  exits 2 with `vector-env: several skills are installed (a, b): give --skill ID or set VECTOR_ENV_SKILL`.
- No skill installed at all: exit 2 with `vector-env: no skill is installed` (`-h` and `--help` print the usage).
- Otherwise it returns `get(id).cli.main(rest, prog="vector-env")`: commands, flags, output and exit codes are
  the skill's. With one skill installed every earlier command line works unchanged, and `-h` with no command
  shows that skill's help.

## 4. Old module paths

The old paths of this section are temporary. They stay through round `bin-picking-expert-r2` and are removed in the first change after that tag.

These import paths stay, each the same module object as the skill module:

| old path | module |
|---|---|
| `vector_env.assets`, `.cli`, `.compose`, `.harness`, `.library`, `.node`, `.policy`, `.rules`, `.scenes`, `.settings`, `.verifier` | `vector_env.skills.bin_picking.<same name>` |
| `vector_env.build` (and `python -m vector_env.build`) | `vector_env.skills.bin_picking.build` |

- Each old path is a module that imports the new one and replaces itself in `sys.modules`:

  ```python
  """Old import path of vector_env.skills.bin_picking.scenes."""
  import sys

  from .skills.bin_picking import scenes as _module


  def __getattr__(name):
      return getattr(_module, name)


  sys.modules[__name__] = _module
  ```

  The module-level `__getattr__` makes a name imported from the old path resolve even when several threads import
  it for the first time at once (a thread may then hold the old module object instead of the new one).

  `vector_env/cli.py` also keeps `if __name__ == "__main__": raise SystemExit(_module.main())` (instead of
  replacing itself when run as a script).
- Patching an attribute through either name patches the same module.
- `vector_env/env.yaml` is a relative symbolic link to `skills/bin_picking/env.yaml`.

## 5. npm package `@vector/env`

The compatibility entries of `exports` (the old specifiers) are temporary. They stay through round `bin-picking-expert-r2` and are removed in the first change after that tag.

`js/package.json` keeps `name`, `version`, `type`, `engines`, `dependencies` (`@vector/sim` from
`file:../../vector-sim/js`) and `license`, and sets:

| specifier | file |
|---|---|
| `.` | `./skills/bin-picking/src/index.js` |
| `./skills/*` | `./skills/*` |
| `./*` | `./skills/bin-picking/src/*` |

- `bin`: `vector-env-sim`, `vector-env-harness`, `vector-env-verify` → `./skills/bin-picking/bin/sim.mjs`,
  `harness.mjs`, `verifier.mjs`.
- `scripts.test`: `node --test test/*.test.js skills/*/test/*.test.js`.
- The code imports `@vector/sim` only by `@vector/sim`, `@vector/sim/<file>.js` and `@vector/sim/testing`.

## 6. `scripts/setup.sh`

- The skill folder is `skills/<VECTOR_ENV_SKILL>/`, else the only `skills/*/` with `example/Dockerfile`.
  Otherwise it exits 1 with one line:
  - `setup: skills/X/example/Dockerfile does not exist (skill folders: skills/a, ...)` (`VECTOR_ENV_SKILL=X`);
  - `setup: no skill folder has an example/Dockerfile (skills/<skill>/example/Dockerfile)`;
  - `setup: several skill folders (skills/a, skills/b): set VECTOR_ENV_SKILL to one of them`.
- The vector-sim pin is `ARG VECTOR_SIM_REF=` of `skills/<skill>/example/Dockerfile`; `VECTOR_SIM_REF` overrides
  it, `VECTOR_SIM_GIT` sets the repository. Without either: exit 1 with `setup: skills/<skill>/example/Dockerfile
  has no ARG VECTOR_SIM_REF=<tag> and VECTOR_SIM_REF is not set`.
- In order: vector-sim beside this checkout (cloned at the pin when missing, else a warning when it is a git
  checkout at another commit), `npm ci` in its `js/`; `.venv` from `requirements.lock` and an editable install of
  this checkout (skipped with `VECTOR_ENV_JS_ONLY=1`); `npm ci` in `js/` and `skills/<skill>/example/`;
  `VECTOR_ENV_SKILL=<skill> vector-env assets pull` (skipped with `VECTOR_ENV_JS_ONLY=1`), which may fail with one
  line saying how to get the bundle; setup still finishes then.

## 7. Packaging and tests

- `[tool.setuptools.package-data]`: `vector_env = ["*.yaml", "skills/*/*.yaml", "skills/*/build/*.yaml"]`.
- `[tool.pytest.ini_options]`: `testpaths = ["tests", "skills"]`, `norecursedirs = ["node_modules", ".*",
  "*.egg-info"]`.
- `tests/` has no `conftest.py`; each skill's `skills/<skill>/tests/` has its own. Test module names are unique
  across the repository. Test modules import their `conftest` only at module level.
- `tests/test_layout.py` checks the registry, the command, each old path being the same object as its module
  (`python -m vector_env.build --help` included), the entry point, `settings` paths, the package data and test
  options, the `vector_env/env.yaml` link, that shared code names no skill, and the test module rules above.
  `tests/test_setup.py` runs `scripts/setup.sh` against local fixtures (section 6).
- `js/test/exports.test.js` checks the package's self-references and that the bins are executable.

```bash
.venv/bin/python -m pytest
cd js && npm test
```

## 8. Rounds

What miners run for a round is the git tag `<leaf-id>-r<n>` in vector-miner, vector-env and vector-sim (round 1 of
`bin-picking-expert`: `bin-picking-expert-r1`). The example's `ARG VECTOR_SIM_REF=` names such a tag. No
`v<number>` tag is used as a pin.

## 9. Behaviour

The restructure changes no result: the same seed gives byte-identical scenes (`scene_id`, `xml`, `meta`,
`checkpoint`, `state_hash`, `target`), the same rows give the same action hashes, outcomes and verifier results,
and every command prints the same output with the same exit code.
