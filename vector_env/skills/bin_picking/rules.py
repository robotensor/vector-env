from __future__ import annotations

import math

from .settings import cell_config, env_config

OUTCOMES = ("success", "missed", "slipped", "knocked_out", "timed_out", "wrong_part")
PILE_OUTCOMES = ("success", "missed", "slipped", "knocked_out", "timed_out")
EXPERT_OUTCOMES = ("expert_error", "wall_timeout", "step_timeout", "disconnected", "image_too_large")
INFRA_OUTCOMES = ("harness_error", "scene_error", "docker_unavailable", "image_missing", "pull_failed", "start_failed",
                  "policy_unavailable", "physics_error")
# a multi-pick (pile) scene that the rules ran to its end: the outcomes are per pick, in result["picks"]
SCENE_DONE = "done"
PICKS_DEFAULT = 10
HARNESS_OUTCOMES = EXPERT_OUTCOMES + INFRA_OUTCOMES
TARGET_CLASSES = ("top", "blocked", "buried", "hidden")
# per-try metrics in every result (and in the verdict of a pile scene)
METRICS = ("outs", "out_parts", "tcp_path_m", "joint_path_rad", "crate_hits", "target_class")
SCORE_KEYS = ("time_weight", "path_weight", "path_ref_m", "out_unit")
MAX_OUTS = 30


def scene_mode(scene: dict | None) -> str:
    """'pile' for pile scenes (meta.config.rules.mode), else 'classic'."""
    r = (((scene or {}).get("meta") or {}).get("config") or {}).get("rules") or {}
    return "pile" if r.get("mode") == "pile" else "classic"


def scene_picks(scene: dict | None) -> int:
    """Picks of a scene: pile scenes `meta.config.rules.picks` (default env.yaml task.pile.picks), else 1."""
    if scene_mode(scene) != "pile":
        return 1
    p = (((scene or {}).get("meta") or {}).get("config") or {}).get("rules", {}).get("picks")
    if isinstance(p, int) and p >= 1:
        return p
    return int(defaults("pile").get("picks") or PICKS_DEFAULT)


def defaults(mode: str | None = None) -> dict:
    """env.yaml `task:`; `mode="pile"` applies its `pile:` overrides (time limit)."""
    t = dict(env_config()["task"])
    pile = t.pop("pile", None) or {}
    if mode == "pile":
        t.update(pile)
    return t


def score_settings(settings: dict | None = None) -> dict:
    """env.yaml `score:`, overridden by `settings`."""
    s = {**(env_config().get("score") or {}), **(settings or {})}
    out = {k: float(s[k]) for k in SCORE_KEYS}
    if not (all(math.isfinite(v) for v in out.values()) and 0 <= out["time_weight"] <= 1 and 0 <= out["path_weight"] <= 1
            and out["path_ref_m"] > 0 and out["out_unit"] >= 0):
        raise ValueError("score: time_weight and path_weight in [0, 1], path_ref_m > 0, out_unit >= 0")
    return out


def task(time_limit_s: float = 15.0, human_factor: float = 2.0, score: dict | None = None) -> dict:
    t, h = float(time_limit_s), float(human_factor)
    if not (math.isfinite(t) and t > 0 and math.isfinite(h) and h >= 1):
        raise ValueError("time_limit_s must be > 0 and human_factor >= 1")
    cfg = cell_config()
    hz = int(cfg["control"]["hz"])
    R = cfg["rules"]
    return {"time_limit_s": t, "human_factor": h, "human_limit_s": t * h, "control_hz": hz, "tick_s": 1.0 / hz,
            "max_rows": math.ceil(t * hz - 1e-9), "hold_s": float(R["hold_s"]), "lift_clear_m": float(R["lift_clear"]),
            "outcomes": list(OUTCOMES), "score": score_settings(score)}


def pick_score(pick: dict, S: dict, lim: float) -> float:
    """One pick's s * f_time * f_path (no outs penalty)."""
    if pick.get("outcome") != "success":
        return 0.0
    t = max(0.0, float(pick.get("t") or 0.0))
    path = max(0.0, float(pick.get("tcp_path_m") or 0.0))
    return round((1 - S["time_weight"] * min(1.0, t / lim)) * (1 - S["path_weight"] * min(1.0, path / S["path_ref_m"])), 6)


def score(result: dict, task: dict | None = None) -> float | None:
    """The score of one scene's result (None when the scene does not count: an infra outcome).

        s      = 1 if success else 0
        f_time = 1 - time_weight * min(1, t / time_limit_s)
        f_path = 1 - path_weight * min(1, tcp_path_m / path_ref_m)
        pen    = out_unit * (2 ** min(outs, 30) - 1)
        score  = s * f_time * f_path - pen

    A multi-pick scene (`result["picks"]`, pile scenes): `(sum over picks of s_i * f_time_i * f_path_i - pen) / P`,
    with `outs` the scene's total and P = `picks_planned`; `time_limit_s` is then the limit of one pick.
    `task` is `task(...)` (its `time_limit_s` and `score` settings); without one, the env.yaml defaults."""
    o = result.get("outcome")
    if o in INFRA_OUTCOMES or result.get("infra"):
        return None
    T = task or {}
    S = score_settings(T.get("score"))
    lim = float(T.get("time_limit_s") or defaults()["time_limit_s"])
    outs = int(result.get("outs") or 0)
    pen = S["out_unit"] * (2 ** min(max(outs, 0), MAX_OUTS) - 1)
    if isinstance(result.get("picks"), list):
        # several picks: (sum of the pick scores - the penalty of the scene's outs) / the planned picks
        P = max(1, int(result.get("picks_planned") or len(result["picks"]) or 1))
        tot = sum(pick_score(p, S, lim) for p in result["picks"])
        return round((tot - pen) / P, 6)
    if o != "success":
        return round(0.0 - pen, 6)
    t = max(0.0, float(result.get("t") or 0.0))
    path = max(0.0, float(result.get("tcp_path_m") or 0.0))
    f_time = 1 - S["time_weight"] * min(1.0, t / lim)
    f_path = 1 - S["path_weight"] * min(1.0, path / S["path_ref_m"])
    return round(f_time * f_path - pen, 6)
