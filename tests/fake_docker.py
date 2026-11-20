import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
if args and args[0] == "kill":
    sys.exit(0)
if args and args[0] == "pull":
    sys.exit(0)


def opt(flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


assert args[0] == "run" and opt("--network") == ["none"] and opt("--pull") == ["never"]
assert "--read-only" in args and "-i" in args and "--rm" in args
assert opt("--cap-drop") == ["ALL"] and opt("--security-opt") == ["no-new-privileges"]
assert opt("--user") == ["65534:65534"] and int(opt("--pids-limit")[0]) > 0 and opt("--memory-swap") == opt("--memory")
assert opt("--tmpfs")[0].startswith("/tmp:") and "size=" in opt("--tmpfs")[0] and "core=0" in opt("--ulimit")
image = args[-1]
mode = image.split(":", 1)[1] if ":" in image else "hold"
os.execv(sys.executable, [sys.executable, os.path.join(HERE, "fake_expert.py"), *mode.split("-")])
