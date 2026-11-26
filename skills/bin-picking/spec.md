# vector-env: bin-picking spec

The bin-picking environment, shared by every bin-picking leaf: scenes, asset bundle, rules, the
`vector-expert/2` harness, the replay verifier, the asset builder and the example expert. Skill id `bin-picking`.

## 1. Files

| path | content |
|---|---|
| `vector_env/skills/bin_picking/__init__.py` | `ID = "bin-picking"`; imports nothing |
| `vector_env/skills/bin_picking/{assets,cli,compose,harness,library,node,policy,rules,scenes,settings,verifier}.py` | the modules |
| `vector_env/skills/bin_picking/env.yaml`, `config.yaml` | task, draw, harness and asset pin; cell physics and rules |
| `vector_env/skills/bin_picking/build/` | the asset builder (`__init__`, `__main__`, `cell`, `demo`, `meshes`, `units`, `usd`, `materials.yaml`) |
| `js/skills/bin-picking/src/` | `coordinator.js`, `env.js`, `index.js`, `io.js`, `trace.js`, `verifier.js` |
| `js/skills/bin-picking/bin/` | `sim.mjs`, `harness.mjs`, `verifier.mjs` (the processes Python talks to) |
| `js/skills/bin-picking/test/` | `env.test.js`, `verifier.test.js`, `fake_expert.mjs`, `fixtures/pick.json` |
| `skills/bin-picking/example/` | the example expert: `solve.mjs`, `package.json`, `package-lock.json`, `Dockerfile`, `README.md` |
| `skills/bin-picking/tests/` | Python tests, their `conftest.py`, helpers and `data/` |
| `skills/bin-picking/README.md` | the environment's documentation |

Modules moved together from `vector_env/`, so relative imports between them are unchanged. The package version is
`vector_env.__version__`.

## 2. Names callers use

These names and their behaviour are unchanged (old and new import paths, see the repository spec):

- `scenes`: `generate`, `draw_index`, `draw_scene`, `seed_for`, `SEED_TRIES`, `env_info`, `scene_cache_root`,
  `KEYS`, `SceneError`.
- `harness`: `run`, `arun`, `replay`, `trace`, `compare`, `summary`, `action_hash`, `act_error`, `docker_argv`,
  `docker_defaults`, `docker_check`, `ensure_image`, `pull`, `image_size`, `manifest_size`, `remove_image`,
  `prepare`, `scene_file`, `stderr_path`, `check_replay`, `CTRL`, `EMPTY_ACTION_HASH`, `HarnessError`,
  `DockerUnavailable`, `ImageMissing`, `PullFailed`, `ImageTooLarge`, `ReplayMismatch`, `ChannelClosed`,
  `Reconnected`.
- `rules`: `OUTCOMES`, `EXPERT_OUTCOMES`, `INFRA_OUTCOMES`, `HARNESS_OUTCOMES`, `defaults`, `task`.
- `assets`: `find`, `ensure`, `bundle_dir`, `cache_root`, `info`, `pull`, `pack`, `write_manifest`, `MISSING`,
  `AssetsError`, `ROOT`, `FORMAT`; logger name `vector_env.assets`.
- `verifier`: `verify`, `verify_result`, `probe`, `prepare`, `pin`, `rows_hash`, `chain_seed`, `chain_next`,
  `reset`, `FORMAT`, `CHAIN_FORMAT`.
- `node`: `NodeProc`, `SimError`, `SimCrashed`, `sim`, `close_thread_sims`.
- `settings`: `env_config`, `cell_config`, `js_dir`, `node`, `HERE`, `ROOT`.
- `cli`: `main`, `report`, `infra_problem`, `draw_scenes`, `scene_cache`, `write_traces`, `EvalError`.
- `compose`: `compose`, `SceneError`; `policy`: `view`, `request`, `rows`, `post`, `PolicyUnavailable`,
  `PolicyRefused`.

Module-level names that callers patch stay module-level and are read at call time: `assets.ROOT`,
`scenes.generate`, `harness.arun`, `harness.run`, `cli.draw_scenes`.

## 3. Paths

- `settings.HERE` is this package's folder; `settings.ROOT` is the checkout root (`HERE.parents[2]`).
- `settings.js_dir()` is `VECTOR_ENV_JS`, else `ROOT / "js"` (the `@vector/env` package root).
- `NodeProc(script, ...)` runs `<js_dir>/skills/bin-picking/bin/<script>`, else `<js_dir>/bin/<script>` when the
  first does not exist; the process's working folder is `<js_dir>`.
- The bundle is found as before: `VECTOR_ENV_BUNDLE` (or `VECTOR_SIM_BUNDLE`), `<ROOT>/bundle`, then
  `VECTOR_ENV_CACHE/<version>` (default `~/.cache/vector-env/assets/<version>`).
- `settings` reads `env.yaml` and `config.yaml` from `HERE`; `build/cell.py` reads `build/materials.yaml` beside it.
- `build.SOURCES` names modules relative to the `build` package (`.usd`, `.demo`); `build/units.py` re-executes
  `python -m vector_env.skills.bin_picking.build` with `ROOT` on `PYTHONPATH`. Its "give its input with --src"
  message names `vector_env/skills/bin_picking/build/<name>.py`.
- `python -m vector_env.build` stays the name the builder's own help (`usage: python -m vector_env.build ...`) and
  the asset index hints of `library.py` give.
- The JS tests set `VECTOR_ENV_DIR` to the checkout root when it is unset.

## 4. Changes

- The verifier's Node processes are the class `verifier.Workers`, reached through `verifier.workers()`.
- `policy` has no format constant. The policy call is `POST <url>/v1/act` with the body of section 6.
- In `compose` the parts a random group draws from are `candidates`; in `build/units.py` the process set is
  `workers`.

## 5. Example expert

- `skills/bin-picking/example/package.json`: `"@vector/sim": "file:../../../../vector-sim/js"` (vector-sim beside
  this checkout); `package-lock.json` regenerated.
- `Dockerfile`: `ARG VECTOR_SIM_GIT=https://github.com/robotensor/vector-sim`, `ARG
  VECTOR_SIM_REF=bin-picking-expert-r1`; fetches vector-sim at the ref into `/vector-sim` and runs `npm ci` in its
  `js/`; `WORKDIR /x/skills/bin-picking/example`; copies `package.json`, `package-lock.json` and `*.mjs`; `npm ci
  --omit=dev`; `ENV VECTOR_SIM_BUNDLE=/bundle HOME=/tmp`; `USER 65534:65534`; `ENTRYPOINT ["node", "solve.mjs"]`.
- `solve.mjs` is unchanged: it sends the same rows for the same messages.

## 6. Protocols

- `vector-expert/2`: unchanged (messages, limits, action hash, docker command).
- Policy calls: the body is `episode`, `call`, `call_in_try`, `meta` (only `camera`, `config`, `crate`, `net`,
  `robot`, `table`, `tick_s`), `target`, `obs`, `qpos` (robot joints only) and `state`. Answers and outcomes are
  unchanged.

## 7. Tests

- `skills/bin-picking/tests/conftest.py`: `HERE` (that folder), `ROOT` (checkout root), `needs_bundle`, `fake(mode,
  *extra)`, the session fixtures `scenes3` and `scene`, and an autouse fixture setting `VECTOR_ENV_SCENE_CACHE` to a
  temporary folder.
- `data/solved.json` replays give the recorded results.
- `tests/test_layout.py` (shared, repository spec section 7) checks that each old path is the same object as its new
  module, that `python -m vector_env.build --help` works, that the package data includes both YAML files, and that
  `vector_env/env.yaml` reads the same as `skills/bin_picking/env.yaml`.
