"""Old import path of vector_env.skills.bin_picking.compose."""
import sys

from .skills.bin_picking import compose as _module


def __getattr__(name):
    return getattr(_module, name)


sys.modules[__name__] = _module
