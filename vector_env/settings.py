"""Old import path of vector_env.skills.bin_picking.settings."""
import sys

from .skills.bin_picking import settings as _module


def __getattr__(name):
    return getattr(_module, name)


sys.modules[__name__] = _module
