from __future__ import annotations

import importlib

SOURCES = {
    "library": "vector_env.build.usd",
    "demo": "vector_env.build.demo",
}


def source(name: str):
    if name not in SOURCES:
        raise KeyError(f"unknown asset source {name!r}; known: {sorted(SOURCES)}")
    return importlib.import_module(SOURCES[name])
