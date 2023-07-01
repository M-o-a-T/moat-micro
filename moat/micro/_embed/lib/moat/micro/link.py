"""
Helper to hot-wire a command to read data from/to the other side.
"""

import sys

from moat.util import OptCtx  # pylint:disable=no-name-in-module
from moat.util.compat import (  # pylint: disable=redefined-builtin,no-name-in-module
    Event,
    TimeoutError,
    every_ms,
    idle,
    ticks_ms,
    ticks_diff,
    wait_for_ms,
)
from .cmd import BaseCmd


class Reader(BaseCmd):
    """
    Base class for something that reads data and (possibly) periodically
    forwards the result to the remote side.

    Config::
        - bg.send: time between messages
        - bg.abs: absolute difference required to trigger sending
        - bg.rel: relative difference
        - t: access the hardware at most every T msec
    """

    _t_send = None  # time between messages

    _d_abs = None
    _d_rel = None
    _d_tm = 0  # required time between hardware read

    _res = None  # data item
    _t_last = None  # time when read

    _r_last = None  # last data item sent
    _r_evt:Event = None  # protect against parallel reads

    def __init__(self, parent, name, cfg, **_kw):
        super().__init__(parent, name)
        self.cfg = cfg
        if isinstance(self, Listener):
            self._d_tm = cfg.get("tr", 0)
            return

        tx = cfg.get("bg", None)
        if tx is not None:
            # send to other side every N msec
            self._t_send = cfg.get("send", None)
            # don't send if absolute delta is < ABS
            self._d_abs = cfg.get("abs", None)
            # don't send if relative delta is < REL.
            r = cfg.get("rel", None)
            if r is not None:
                r = 1+r  # r=0.1: 10% more
            self._d_rel = r

            # take local read from cache if not older than T
            self._d_tm = cfg.get("t", 0)

    async def cmd(self):
        """read data"""
        return await self.read_()


    async def run(self):
        "background worker"
        if self._t_send is not None:
            async for r in every_ms(self._t_send, self._r_delta):
                pass
        else:
            await idle()

    async def _r_delta(self):
        "read-and-send, called periodically. Uses cache if available"
        r = await self.read()
        rp = self._r_last
        if rp is None:
            pass
        elif self._d_abs is not None and -self._d_abs < r-rp < self._d_abs:
            return
        elif rp and self._d_rel and 1/self._d_rel < r/rp < self._d_rel:
            return
        self._r_last = r
        await self.send(r)

    async def read(self, wait=False):
        """read if out of date.

        If NOT out of date, wait if @wait is True, else return "stale" data.
        """
        t = ticks_ms()
        if self._res is not None and (td := ticks_diff(t, self._t_last)) < self._d_tm:
            if not wait:
                return self._res
            dly = self._d_tm - td
        else:
            dly = 0

        if self._r_evt is None:
            self._r_evt = Event()
            if dly:
                await sleep_ms(dly)
            try:
                r = await self.read_()
                self.set(r)
                return r
            except BaseException:
                self._res = None
                raise
            finally:
                self._r_evt.set()
                self._r_evt = None
        else:
            await self._r_evt.wait()
            if self._res is None:
                raise RuntimeError("No read: " + ".".join(self.path))
            
        return self._res

    async def read_(self):
        "the actual `read()` function` you need to override"
        raise NotImplementedError("Reader")

    def set(self, data):
        self._res = data
        self._t_last = ticks_ms()

    async def send(self, data):
        "update the remote side"
        await self.request.send_nr(self.path, d=data)


class Listener(Reader):
    """
    Link object that listens to a specific message from the other side.

    Reading returns the latest/next message.

    Parameters:
        - link_age: timeout. Ask for data when none have been seen within
          this many seconds.
    """

    # pylint: disable=abstract-method

    _up = None
    _rd = None
    _dt = 100  # 100ms
    _t = None

    def __init__(self, parent, path, cfg, **kw):
        super().__init__(parent, path, cfg, **kw)

        self._up = Event()
        if "link_age" in cfg:
            self._dt = cfg.link_age * 1000

    async def run(self):
        "nothing to do here"
        pass

    async def read_(self):
        """
        Wait for new data.
        """
        return await self.request.send(self.path)

    async def cmd(self, d=None):
        if d is None:  # read
            return await self.read_()
        else:  # set data
            self._rd = d
            self._t = ticks_ms()
            self._up.set()
            self._up = Event()

    async def send(self, data):
        """
        don't send: we received it!
        """
        pass  # pylint:disable=unnecessary-pass
