"""Old import path of vector_env.skills.bin_picking.cli."""
import sys

from .skills.bin_picking import cli as _module


def __getattr__(name):
    return getattr(_module, name)


if __name__ == "__main__":
    raise SystemExit(_module.main())
sys.modules[__name__] = _module
