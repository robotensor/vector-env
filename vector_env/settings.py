from __future__ import annotations

import copy
import os
from functools import lru_cache
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


@lru_cache(None)
def _yaml(name: str) -> dict:
    return yaml.safe_load((HERE / name).read_text())


def env_config() -> dict:
    return copy.deepcopy(_yaml("env.yaml"))


@lru_cache(None)
def _cell() -> dict:
    cfg = copy.deepcopy(_yaml("config.yaml"))
    cfg["rules"]["try_s"] = float(_yaml("env.yaml")["task"]["time_limit_s"])
    return cfg


def cell_config() -> dict:
    return _cell()


def js_dir() -> Path:
    return Path(os.environ.get("VECTOR_ENV_JS") or ROOT / "js")


def node() -> str:
    return os.environ.get("VECTOR_ENV_NODE") or "node"
