import asyncio
import random

from policy import track

from vector_env.harness import ChannelClosed, Reconnected


class FakeMiner:
    def __init__(self, per=5, seed=0, noise=False, delay_s=(0.0, 0.004), stall=None, flood=False, error=False,
                 close_after=None, reconnect_after=None, junk=False, broken_after=None, drop=None, first_delay_s=0.0):
        self.per, self.noise, self.delay_s = per, noise, delay_s
        self.rng = random.Random(seed)
        self.stall, self.flood, self.error, self.junk = stall or {}, flood, error, junk
        self.close_after, self.reconnect_after, self.broken_after = close_after, reconnect_after, broken_after
        self.drop, self.first_delay_s = drop, first_delay_s
        self.paused = False
        self.drops = 0
        self.inbox = None
        self.seen, self.rows, self.arm, self.ends = [], {}, {}, {}
        self.acts = 0
        self.closed = False

    def _q(self):
        if self.inbox is None:
            self.inbox = asyncio.Queue()
        return self.inbox

    async def send(self, msg):
        if self.closed:
            raise ChannelClosed("gone")
        self.seen.append((msg["type"], msg["scene_key"]))
        key = msg["scene_key"]
        if msg["type"] == "end":
            self.ends[key] = msg
            return
        if msg["type"] == "start":
            self.arm[key] = msg["scene"]["meta"]["robot"]["arm_joints_qpos"]
            self.rows[key] = []
            if self.junk:
                self._q().put_nowait({"type": "act", "scene_key": "nobody", "rows": []})
                self._q().put_nowait("not a dict")
            if self.error:
                self._q().put_nowait({"type": "error", "scene_key": key, "error": "cannot plan"})
                return
            if self.flood:
                for _ in range(8):
                    self._q().put_nowait({"type": "act", "scene_key": key, "rows": [], "obs": False})
                return
        if any(key.startswith(p) and msg["tick"] >= t for p, t in self.stall.items()):
            return
        rows = track(msg, self.arm[key], self.per)
        if self.noise:
            rows = [[x + random.uniform(-1e-9, 1e-9) for x in r] for r in rows]
        self.rows[key] += rows
        extra = self.first_delay_s if msg["type"] == "start" else 0.0
        asyncio.get_running_loop().create_task(self._reply(key, rows, extra))

    async def _reply(self, key, rows, extra=0.0):
        await asyncio.sleep(self.rng.uniform(*self.delay_s) + extra)
        self._q().put_nowait({"type": "act", "scene_key": key, "rows": rows, "obs": False})

    async def recv(self):
        m = await self._q().get()
        self.acts += 1
        if self.drop is not None and self.acts == self.drop[0]:
            self.paused = True
            self.drops += 1
            await asyncio.sleep(self.drop[1])
            self.paused = False
            self.rows = {k: [] for k in self.rows}
            raise Reconnected()
        if self.reconnect_after is not None and self.acts == self.reconnect_after:
            self.rows = {k: [] for k in self.rows}
            raise Reconnected()
        if self.close_after is not None and self.acts >= self.close_after:
            self.closed = True
            raise ChannelClosed("socket dropped")
        if self.broken_after is not None and self.acts >= self.broken_after:
            raise KeyError("bug on our side")
        return m
