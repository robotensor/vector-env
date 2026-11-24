"""A test expert that sends the rows of a recorded run (tests/data/solved.json) for the scene it was recorded on."""
import base64
import json
import struct
import sys
from pathlib import Path

DATA = json.loads((Path(__file__).resolve().parent / "data" / "solved.json").read_text())
raw = base64.b64decode(DATA["ctrl"])
ROWS = [list(struct.unpack("<7d", raw[k:k + 56])) for k in range(0, len(raw), 56)]
rows = []


def send(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    m = json.loads(line)
    if m["type"] == "end":
        break
    if m["type"] == "start":
        if m["scene"]["scene_id"] != DATA["scene_id"]:
            send({"type": "error", "error": f"no recorded run for {m['scene']['scene_id']}"})
            continue
        rows = ROWS[:]
    send({"type": "act", "rows": rows[:20], "obs": False})
    rows = rows[20:]
