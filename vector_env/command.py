"""The vector-env command: `vector-env [--skill ID] <command> [args...]` and `vector-env skills`."""
from __future__ import annotations

import importlib
import json
import sys

from . import skills

USAGE = """usage: vector-env [--skill ID] <command> [args...]
       vector-env skills

Runs a command of one skill. Without --skill the skill is $VECTOR_ENV_SKILL, else the only installed skill.

  skills       print the installed skills and the default one as one JSON line
  --skill ID   the skill whose command runs (given before the command)
"""


def _fail(msg: str) -> int:
    print(f"vector-env: {msg}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    chosen = None
    if args and args[0] == "--skill":
        if len(args) < 2:
            return _fail("--skill needs an ID")
        chosen, args = args[1], args[2:]
    elif args and args[0].startswith("--skill="):
        chosen, args = args[0].split("=", 1)[1], args[1:]
    if args[:1] == ["skills"]:
        print(json.dumps({"skills": list(skills.available()), "default": skills.default()}))
        return 0
    skill_id = chosen or skills.default()
    if skill_id is None:
        installed = ", ".join(skills.available())
        if args[:1] in (["-h"], ["--help"]):
            print(USAGE, end="")
            return 0
        if not installed:
            return _fail("no skill is installed")
        return _fail(f"several skills are installed ({installed}): give --skill ID or set VECTOR_ENV_SKILL")
    try:
        package = skills.get(skill_id)
    except KeyError as e:
        return _fail(e.args[0])
    cli = importlib.import_module(f"{package.__name__}.cli")
    return cli.main(args, prog="vector-env")
