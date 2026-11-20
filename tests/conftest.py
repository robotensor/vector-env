import sys
from pathlib import Path

import pytest

from vector_env import assets

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
needs_bundle = pytest.mark.skipif(assets.find() is None, reason=assets.MISSING)


@pytest.fixture(scope="session")
def scenes3():
    from vector_env import scenes
    if assets.find() is None:
        pytest.skip(assets.MISSING)
    return [scenes.generate(s) for s in (1, 2, 3)]


@pytest.fixture(scope="session")
def scene(scenes3):
    return scenes3[0]


def fake(mode: str, *extra: str) -> dict:
    return {"command": [sys.executable, str(HERE / "fake_expert.py"), mode, *extra]}
