def track(msg: dict, arm: list[int], per: int) -> list[list[float]]:
    q = [msg["qpos"][i] for i in arm]
    k = int(msg["tick"])
    return [[q[j] + 0.003 * ((k + r + j) % 7) - 0.009 for j in range(6)] + [0.0] for r in range(per)]
