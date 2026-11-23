from __future__ import annotations

import math

from .settings import cell_config, env_config

OUTCOMES = ("success", "missed", "slipped", "knocked_out", "timed_out", "wrong_part")
EXPERT_OUTCOMES = ("expert_error", "wall_timeout", "image_too_large")
INFRA_OUTCOMES = ("harness_error", "scene_error", "docker_unavailable", "image_missing", "pull_failed", "start_failed")
HARNESS_OUTCOMES = EXPERT_OUTCOMES + INFRA_OUTCOMES


def defaults() -> dict:
    return dict(env_config()["task"])


def task(time_limit_s: float = 15.0, human_factor: float = 2.0) -> dict:
    t, h = float(time_limit_s), float(human_factor)
    if not (math.isfinite(t) and t > 0 and math.isfinite(h) and h >= 1):
        raise ValueError("time_limit_s must be > 0 and human_factor >= 1")
    cfg = cell_config()
    hz = int(cfg["control"]["hz"])
    R = cfg["rules"]
    return {"time_limit_s": t, "human_factor": h, "human_limit_s": t * h, "control_hz": hz, "tick_s": 1.0 / hz,
            "max_rows": math.ceil(t * hz - 1e-9), "hold_s": float(R["hold_s"]), "lift_clear_m": float(R["lift_clear"]),
            "outcomes": list(OUTCOMES)}
