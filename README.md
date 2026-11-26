# vector-env

The task environment of each Vector skill: the scene generator, the asset bundle, the task rules and the expert
step harness. The simulator (vector-sim) runs here, outside the expert. One Python package, `vector_env`, and one
npm package, `@vector/env`, hold every skill; each skill has its own folders and its own README.

## Skills

| skill | docs | Python | Node | shorter paths |
|---|---|---|---|---|
| `bin-picking` | [skills/bin-picking/README.md](skills/bin-picking/README.md) | `vector_env.skills.bin_picking` | `js/skills/bin-picking/` | `vector_env.<module>`, `vector_env/env.yaml`, `@vector/env` |

## Layout

| path | what |
|---|---|
| `vector_env/__init__.py` | `__version__` |
| `vector_env/__main__.py` | `python -m vector_env` = the `vector-env` command |
| `vector_env/command.py` | the `vector-env` command |
| `vector_env/skills/__init__.py` | the skill registry: `available()`, `get(id)`, `default()` |
| `vector_env/skills/<skill_py>/` | one skill's Python code and data |
| `vector_env/<module>.py`, `vector_env/build/`, `vector_env/env.yaml` | the shorter paths (below): the modules and `env.yaml` of the skill marked in the skills table |
| `js/package.json`, `js/package-lock.json` | `@vector/env` |
| `js/skills/<skill>/{src,bin,test}/` | one skill's Node code |
| `js/test/` | tests of the npm package itself |
| `skills/<skill>/` | one skill's `README.md`, `spec.md`, `example/` and `tests/` |
| `tests/` | shared tests: registry, command, import paths, `setup.sh` |
| `scripts/setup.sh`, `pyproject.toml`, `requirements.lock`, `README.md`, `spec.md` | the repository |

`spec.md` is the repository's spec; `skills/<skill>/spec.md` is each skill's.

## Setup

```bash
git clone https://github.com/robotensor/vector-env && cd vector-env
scripts/setup.sh
```

`setup.sh`, in order:

1. vector-sim beside this folder (`../vector-sim`), cloned at the round tag `ARG VECTOR_SIM_REF=` of
   `skills/<skill>/example/Dockerfile` when it is missing, then `npm ci` in its `js/`. A vector-sim already there
   is kept; when it is a git checkout at another commit, setup prints a warning with the `git checkout` that fixes
   it.
2. `.venv` from `requirements.lock` (every package pinned with its hashes, installed with `--require-hashes`) and an
   editable install of this checkout. It uses `uv` when it is installed, else `python3 -m venv`.
3. The Node parts of this checkout: `npm ci` in `js/` and in `skills/<skill>/example/`.
4. The asset bundle (`vector-env assets pull`). Without an asset source setup still finishes and prints one line
   saying how to get the bundle (`VECTOR_ENV_ASSETS_SOURCE` or `VECTOR_ENV_BUNDLE`, then `vector-env assets pull`).

| variable | effect |
|---|---|
| `VECTOR_ENV_SKILL` | the skill folder setup uses (`skills/<skill>/`); needed only when several skills have an `example/Dockerfile` |
| `VECTOR_SIM_REF` | overrides the pin (a tag, a branch or a full commit sha) |
| `VECTOR_SIM_GIT` | the vector-sim repository to clone |
| `VECTOR_ENV_JS_ONLY=1` | only the Node parts: no `.venv`, no bundle |

When setup cannot pick a skill folder it exits 1 with one line:

- `setup: skills/X/example/Dockerfile does not exist (skill folders: ...)`: `VECTOR_ENV_SKILL=X` names no skill
  with an example;
- `setup: several skill folders (skills/a, skills/b): set VECTOR_ENV_SKILL to one of them`;
- `setup: no skill folder has an example/Dockerfile (skills/<skill>/example/Dockerfile)`.

Python 3.12 (`requires-python >=3.12`, `.python-version`), Node 22 or newer (`.nvmrc`: 24). Miners usually get this
package through vector-miner's `scripts/setup.sh`.

## The command

```bash
vector-env [--skill ID] <command> [args...]
vector-env skills
```

- `vector-env skills` prints the installed skills and the default one as one JSON line, e.g.
  `{"skills": ["bin-picking"], "default": "bin-picking"}`.
- `--skill ID` (or `--skill=ID`) comes before the command. Without it the skill is `VECTOR_ENV_SKILL`, else the
  only skill installed. With one skill installed every command works without `--skill`.
- An unknown skill exits 2 with `vector-env: unknown skill X (installed: ...)`. With several skills installed and
  none chosen, `vector-env -h` prints the usage above and any other command exits 2 with
  `vector-env: several skills are installed (...): give --skill ID or set VECTOR_ENV_SKILL`.
- The commands, flags, output and exit codes are the skill's: see its README.
- `python -m vector_env` is the same command.

## Python

```python
from vector_env import skills

skills.available()          # {skill id: package}, sorted by id
skills.get("<skill>")       # one skill's package (KeyError for an unknown id)
skills.default()            # VECTOR_ENV_SKILL, else the only skill, else None
```

A skill's modules are `vector_env.skills.<skill_py>.<module>` (`<skill_py>` is the id with `_` for `-`). The
shorter names `vector_env.<module>` (`vector_env.scenes`, `vector_env.harness`, ..., `vector_env.build`) keep
working for the skill marked in the skills table: each is the same module object as the skill module, so patching
through either name patches both. `vector_env/env.yaml` is a link to that skill's `env.yaml`, and `@vector/env`
(and `@vector/env/<file>.js`) are its Node modules.

## Rounds

What miners run for a round is the git tag `<leaf-id>-r<n>` in vector-miner, vector-env and vector-sim. Round 1 of
`bin-picking-expert` is `bin-picking-expert-r1`. Each example's `ARG VECTOR_SIM_REF=` names such a tag.

## Tests

```bash
.venv/bin/python -m pytest       # tests/ and skills/*/tests/
cd js && npm test                # js/test/ and js/skills/*/test/
```

Tests that need the asset bundle skip with the reason when there is none.

## License

Apache-2.0.
