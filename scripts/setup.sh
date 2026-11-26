#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
found=()
for f in skills/*/example/Dockerfile; do
  [ -f "$f" ] && found+=("${f%/example/Dockerfile}")
done
list=$(IFS=,; echo "${found[*]:-}" | sed 's/,/, /g')
if [ -n "${VECTOR_ENV_SKILL:-}" ]; then
  SKILL_DIR=skills/$VECTOR_ENV_SKILL
  if [ ! -f "$SKILL_DIR/example/Dockerfile" ]; then
    echo "setup: $SKILL_DIR/example/Dockerfile does not exist (skill folders: ${list:-none})" >&2
    exit 1
  fi
elif [ "${#found[@]}" -eq 1 ]; then
  SKILL_DIR=${found[0]}
elif [ "${#found[@]}" -eq 0 ]; then
  echo "setup: no skill folder has an example/Dockerfile (skills/<skill>/example/Dockerfile)" >&2
  exit 1
else
  echo "setup: several skill folders ($list): set VECTOR_ENV_SKILL to one of them" >&2
  exit 1
fi
SKILL=${SKILL_DIR#skills/}
SIM=../vector-sim
SIM_REF=${VECTOR_SIM_REF:-$(sed -n 's/^ARG VECTOR_SIM_REF=\([^[:space:]]*\).*/\1/p' "$SKILL_DIR/example/Dockerfile")}
if [ -z "$SIM_REF" ]; then
  echo "setup: $SKILL_DIR/example/Dockerfile has no ARG VECTOR_SIM_REF=<tag> and VECTOR_SIM_REF is not set" >&2
  exit 1
fi
check_ref() {
  local dir=$1 ref=$2 name=$3 have want
  have=$(git -C "$dir" rev-parse -q --verify HEAD 2>/dev/null) || return 0
  want=$(git -C "$dir" rev-parse -q --verify "$ref^{commit}" 2>/dev/null) || want=
  if [ -z "$want" ]; then
    echo "setup: warning: $dir does not have $ref; fetch it (git -C $dir fetch --tags) and check it out, or local runs use another $name than the round's" >&2
  elif [ "$have" != "$want" ]; then
    echo "setup: warning: $dir is at ${have:0:12}, not $ref; local runs then use another $name than the round's (git -C $dir checkout $ref)" >&2
  fi
}
if [ ! -d "$SIM/js" ]; then
  git clone --depth 1 --branch "$SIM_REF" "${VECTOR_SIM_GIT:-https://github.com/robotensor/vector-sim}" "$SIM"
else
  check_ref "$SIM" "$SIM_REF" vector-sim
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
(cd "$SKILL_DIR/example" && npm ci --no-audit --no-fund)
if [ -z "${VECTOR_ENV_JS_ONLY:-}" ] && ! err=$(VECTOR_ENV_SKILL=$SKILL .venv/bin/vector-env assets pull 2>&1 >/dev/null); then
  echo "assets: not installed yet (${err#vector-env: }); set VECTOR_ENV_ASSETS_SOURCE (hf://<repo> or a folder) or VECTOR_ENV_BUNDLE, then .venv/bin/vector-env assets pull" >&2
fi
