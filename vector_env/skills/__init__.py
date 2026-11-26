"""The skill registry: every subpackage of vector_env.skills whose ID matches its folder name."""
from __future__ import annotations

import importlib
import os
import pkgutil
import types


def available() -> dict[str, types.ModuleType]:
    """{skill id: skill package} of every installed skill, sorted by id."""
    found = {}
    for info in pkgutil.iter_modules(__path__):
        if not info.ispkg:
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        skill_id = getattr(mod, "ID", None)
        if isinstance(skill_id, str) and skill_id.replace("-", "_") == info.name:
            found[skill_id] = mod
    return dict(sorted(found.items()))


def get(skill_id: str) -> types.ModuleType:
    """The package of the skill `skill_id`; KeyError naming the installed ids when there is none."""
    skills = available()
    if skill_id not in skills:
        raise KeyError(f"unknown skill {skill_id} (installed: {', '.join(skills)})")
    return skills[skill_id]


def default() -> str | None:
    """$VECTOR_ENV_SKILL, else the only installed skill, else None."""
    chosen = os.environ.get("VECTOR_ENV_SKILL")
    if chosen:
        return chosen
    skills = available()
    return next(iter(skills)) if len(skills) == 1 else None
