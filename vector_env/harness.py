"""Old import path of vector_env.skills.bin_picking.harness."""
import sys

from .skills.bin_picking import harness as _module


def __getattr__(name):
    return getattr(_module, name)


sys.modules[__name__] = _module
