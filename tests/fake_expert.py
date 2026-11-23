import json
import os
import sys
import time
from pathlib import Path

mode = sys.argv[1] if len(sys.argv) > 1 else "hold"
per = int(sys.argv[2]) if len(sys.argv) > 2 and mode != "sleep" else 1
q = None
asked = False
if mode == "noisy":
    sys.stderr.write("noisy expert says hi\n")
    sys.stderr.flush()
    mode = "empty"


def send(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    m = json.loads(line)
    if m["type"] == "end":
        break
    if m["type"] == "start":
        arm = m["scene"]["meta"]["robot"]["arm_joints_qpos"]
        q = [m["qpos"][i] for i in arm] + [0.0]
        assert m["format"] == "vector-expert/2" and m["obs"]["width"] > 0 and m["limits"]["max_rows"] > 0
        if mode == "crash":
            sys.exit(3)
        if mode == "junk":
            print("hello", flush=True)
            continue
        if mode == "sleep":
            if len(sys.argv) > 2:
                Path(sys.argv[2]).write_text(str(os.getpid()))
            time.sleep(3600)
        if mode == "error":
            send({"type": "error", "error": "cannot plan"})
            continue
        if mode == "longline":
            sys.stdout.write("x" * (2 << 20) + "\n")
            sys.stdout.flush()
            time.sleep(3600)
        if mode == "nonl":
            while True:
                sys.stdout.write("y" * 65536)
                sys.stdout.flush()
        if mode == "flood":
            for _ in range(50):
                send({"type": "act", "rows": [q], "obs": False})
            time.sleep(3600)
        if mode == "badrow":
            send({"type": "act", "rows": [[0, 1, 2]]})
            continue
    if mode == "obs" and m["type"] == "state" and not asked:
        sys.exit(0 if "obs" in m and m["obs"]["depth"] else 4)
    if mode == "empty":
        send({"type": "act", "rows": []})
    elif mode == "obs":
        asked = asked or m["type"] == "state"
        send({"type": "act", "rows": [q], "obs": m["type"] == "start"})
    else:
        send({"type": "act", "rows": [q] * per, "obs": False})
