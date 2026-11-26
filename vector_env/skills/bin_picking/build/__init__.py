from __future__ import annotations

import importlib

SOURCES = {
    "library": ".usd",
    "demo": ".demo",
}


def source(name: str):
    if name not in SOURCES:
        raise KeyError(f"unknown asset source {name!r}; known: {sorted(SOURCES)}")
    return importlib.import_module(SOURCES[name], __package__)
