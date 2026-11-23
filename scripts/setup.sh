#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
SIM=../vector-sim
if [ ! -d "$SIM/js" ]; then
  git clone --depth 1 --branch "${VECTOR_SIM_REF:-main}" "${VECTOR_SIM_GIT:-https://github.com/robotensor/vector-sim}" "$SIM"
fi
(cd "$SIM/js" && npm ci --no-audit --no-fund)
if [ -z "${VECTOR_ENV_JS_ONLY:-}" ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv --allow-existing .venv --python "${PYTHON:-3.12}"
    uv pip install --python .venv/bin/python --require-hashes -r requirements.lock
    uv pip install --python .venv/bin/python --no-deps -e .
  else
    "${PYTHON:-python3}" -m venv .venv
    .venv/bin/pip install --require-hashes -r requirements.lock
    .venv/bin/pip install --no-deps -e .
  fi
fi
(cd js && npm ci --no-audit --no-fund)
(cd example && npm ci --no-audit --no-fund)
if [ -z "${VECTOR_ENV_JS_ONLY:-}" ] && ! err=$(.venv/bin/vector-env assets pull 2>&1 >/dev/null); then
  echo "assets: not installed yet (${err#vector-env: }); set VECTOR_ENV_ASSETS_SOURCE (hf://<repo> or a folder) or VECTOR_ENV_BUNDLE, then .venv/bin/vector-env assets pull" >&2
fi
