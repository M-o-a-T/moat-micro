"""
Readers that aggregate multiple results
"""

from moat.util import attrdict, load_from_cfg
from moat.util.compat import TaskGroup

from moat.micro.link import Reader


class Array(Reader):
    """
    A generic reader that builds a list of values

    Configuration:
    - default: common parameter for all parts
      typically includes "client" or "server" tags
    - parts: array with separate config for paths
      typically includes pin numbers
    """

    PARTS = "parts"
    ATTR = None  # if the part isn't a dict

    def __init__(self, cfg, **kw):
        super().__init__(cfg, **kw)

        self.parts = []

        std = cfg.get("default", {})
        for p in cfg[self.PARTS]:
            if not isinstance(p, dict):
                if self.ATTR is None:
                    raise ValueError(p)
                p = attrdict(**{self.ATTR: p})
            for k, v in std.items():
                p.setdefault(k, v)

            self.parts.append(load_from_cfg(p, _raise=True, **kw))

    async def run(self, cmd):
        "Start the parts' background tasks"
        async with TaskGroup() as tg:
            for p in self.parts:
                await tg.spawn(p.run, cmd)

    async def read(self):
        """
        Return all values as an array
        """
        res = [None] * len(self.parts)

        async def proc(n):
            r = await self.parts[n]
            res[n] = r

        async with TaskGroup() as tg:
            for i in range(len(self.parts)):
                tg.start_soon(proc, i)
        return res


class Subtract(Reader):
    """
    A generic reader that returns a relative value.
    """

    def __init__(self, cfg, **kw):
        pin = cfg.pin
        ref = cfg.ref
        if not isinstance(ref, dict):
            ref = attrdict(pin=ref)

        for k, v in pin.items():
            ref.setdefault(k, v)

        self.pos = load_from_cfg(pin, **kw)
        self.neg = load_from_cfg(ref, **kw)

    async def run(self, cmd):
        async with TaskGroup() as tg:
            await tg.spawn(self.pos.run, cmd)
            await tg.spawn(self.neg.run, cmd)

    async def read_(self):
        p = n = None

        async def get_rel():
            nonlocal n
            n = await self.neg.read()

        self._tg.start_soon(get_rel)
        p = await self.pos.read()

        return p - n


class MultiplyDict(Reader):
    """
    Measure/aggregate data by multiplying two (or more) readouts.

    Useful e.g. for power (separate channels for U and I).

    Returns a dict with all input values; the product is stored as the key '_'.
    """

    def __init__(self, cfg, **kw):
        super().__init__(cfg, **kw)
        self.sub = cfg.sub
        self.rdr = {}
        for k in self.sub:
            self.rdr[k] = load_from_cfg(cfg[k])

    async def read_(self):
        res = {}

        async with TaskGroup() as tg:

            async def rd(k):
                res[k] = await self.rdr[k].read()

            for k in self.sub:
                tg.start_soon(rd, k)

        r = 1
        for v in res.values():
            r *= v
        res["_"] = v
        return res


class Multiply(MultiplyDict):
    "A multiplier that returns just the result"

    async def read_(self):
        "returns just the product"
        res = await super().read_()
        return res["_"]
