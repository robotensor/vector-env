from __future__ import annotations

import json
import urllib.error
import urllib.request

META_KEYS = ("camera", "config", "crate", "net", "robot", "table", "tick_s")
CTRL = 7


class PolicyUnavailable(Exception):
    pass


class PolicyRefused(Exception):
    pass


def view(start: dict, msg: dict) -> dict:
    meta = (start.get("scene") or {}).get("meta") or {}
    R = meta.get("robot") or {}
    idx = [int(i) for i in R.get("arm_joints_qpos") or []]
    if R.get("driver_qpos") is not None:
        idx.append(int(R["driver_qpos"]))
    n = max(idx) + 1 if idx else 0
    return {"meta": {k: meta[k] for k in META_KEYS if k in meta}, "target": start.get("target"),
            "obs": msg.get("obs") or {}, "qpos": list((msg.get("qpos") or [])[:n])}


def request(start: dict, msg: dict, k: int, state, episode: str = "policy") -> dict:
    scene_id = (start.get("scene") or {}).get("scene_id")
    return {"episode": f"{episode}:{scene_id}", "call": f"c{k}", "call_in_try": k, **view(start, msg), "state": state}


def rows(ctrl) -> list[list[float]]:
    if not ctrl:
        return []
    if isinstance(ctrl[0], (list, tuple)):
        return [[float(x) for x in r] for r in ctrl]
    flat = [float(x) for x in ctrl]
    return [flat[i:i + CTRL] for i in range(0, len(flat) - len(flat) % CTRL, CTRL)]


def post(url: str, token: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url.rstrip("/") + "/v1/act", data=json.dumps(body, separators=(",", ":")).encode(),
                                 headers={"content-type": "application/json",
                                          **({"authorization": f"Bearer {token}"} if token else {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode(errors="replace")
        if e.code in (400, 404, 413, 422):
            raise PolicyRefused(f"the policy refused the request ({e.code}): {detail}") from e
        raise PolicyUnavailable(f"the policy answered {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PolicyUnavailable(f"no answer from the policy within {timeout:g} s: {getattr(e, 'reason', e)}") from e
    try:
        j = json.loads(raw)
    except ValueError as e:
        raise PolicyRefused("the policy's answer is not JSON") from e
    if not isinstance(j, dict):
        raise PolicyRefused("the policy's answer is not a JSON object")
    return j
