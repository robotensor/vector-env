"""Old import path of vector_env.skills.bin_picking.scenes."""
import sys

from .skills.bin_picking import scenes as _module


def __getattr__(name):
    return getattr(_module, name)


sys.modules[__name__] = _module
