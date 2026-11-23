import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
STATE = os.environ.get("FAKE_DOCKER_STATE")
st = json.load(open(STATE)) if STATE else {"daemon": True, "local": None}


def save():
    if STATE:
        st.setdefault("calls", []).append(args[:2])
        json.dump(st, open(STATE, "w"))


def local():
    return st.get("local")


if args and args[0] == "kill":
    sys.exit(0)
if not st.get("daemon", True):
    save()
    sys.stderr.write("Cannot connect to the Docker daemon\n")
    sys.exit(1)
if args[:1] == ["version"]:
    print("27.0.0")
    sys.exit(0)
if args[:2] == ["image", "inspect"]:
    have = local()
    if have is not None and args[-1] not in have:
        sys.stderr.write("No such image\n")
        sys.exit(1)
    print(1000 if have is None else have[args[-1]])
    sys.exit(0)
if args[:2] == ["image", "rm"]:
    (local() or {}).pop(args[-1], None)
    save()
    sys.exit(0)
if args[:2] == ["manifest", "inspect"]:
    size = (st.get("remote") or {}).get(args[-1])
    save()
    if size is None:
        sys.stderr.write("no such manifest\n")
        sys.exit(1)
    layer = {"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "size": size - 100}
    entry = lambda arch, n: {"Descriptor": {"platform": {"architecture": arch, "os": "linux"}},
                             "OCIManifest": {"config": {"size": 100}, "layers": [{**layer, "size": n - 100}]}}
    print(json.dumps([entry("amd64", size), entry("arm64", size), entry("unknown", 500)]))
    sys.exit(0)
if args and args[0] == "pull":
    size = (st.get("remote") or {}).get(args[-1])
    if size is None or args[-1] in (st.get("pull_fail") or []):
        save()
        sys.stderr.write("pull access denied\n")
        sys.exit(1)
    st.setdefault("local", {})[args[-1]] = (st.get("unpacked") or {}).get(args[-1], 2 * size)
    save()
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
if local() is not None and image not in local():
    sys.stderr.write("docker: Error response from daemon: No such image\n")
    sys.exit(125)
if mode == "nostart":
    sys.stderr.write("docker: Error response from daemon: failed to create the container\n")
    sys.exit(125)
if opt("--cidfile"):
    open(opt("--cidfile")[0], "w").write("f" * 64)
if mode == "exit125":
    sys.stderr.write("docker: Error response from daemon: pretend\n")
    sys.exit(125)
os.execv(sys.executable, [sys.executable, os.path.join(HERE, "fake_expert.py"), *mode.split("-")])
