"""
Helper to hot-wire a command to read data from/to the other side.
"""

from moat.util import OptCtx  # pylint:disable=no-name-in-module

from moat.micro.compat import (  # pylint: disable=redefined-builtin,no-name-in-module
    Event,
    every_ms,
    TimeoutError,
    idle,
    wait_for_ms,
)


class Reader:
    """
    Base class for something that reads data.

    The "send" method forwards to the other side.

    Config::
        - link: name of the data item
        - t_send: time between messages
    """

    _link = None
    __cmd = None
    _t_send = None
    _res = None

    def __init__(self, cfg, **_kw):
        self.cfg = cfg
        self._link = cfg.get("link", None)
        self._t_send = cfg.get("t_send", None)

    async def run(self, cmd):
        "background worker"
        self.__cmd = cmd
        reg = cmd.base.register(self, cmd.name, self._link) if self._link is not None else None
        with OptCtx(reg):
            td = self.cfg.get("t_send", None)
            if td:
                async for _ in every_ms(td, self._ras):
                    pass
            else:
                await idle()

    async def _ras(self):
        "read-and-send, called periodically. Uses cache if available"
        r = self._res
        if r is None:
            r = await self.read_()
        else:
            self._res = None
        await self.send(r)

    async def read(self):
        "read and maybe-send, called externally. Not cached"
        res = await self.read_()
        if self._t_send:
            self._res = res
        else:
            await self.send(res)
        return res

    async def read_(self):
        "the actual `read()` function` you need to override"
        raise NotImplementedError("Reader")

    async def send(self, msg):
        "send to the remote side; called by `read`"
        if self._link is None:
            return
        if self.__cmd is None:
            return

        await self.__cmd.request.send_nr("s", o=(self.__cmd.name, self._link), d=msg)


class Listener(Reader):
    """
    Link object that listens to a specific message from the other side.

    Reading returns the latest/next message.
    """

    # pylint: disable=abstract-method

    _cmd = None
    _up = None
    _rd = None
    #  _link = None  # BaseReader

    def __init__(self, cfg, **kw):
        super().__init__(cfg, **kw)
        self._up = Event()

    async def run(self, cmd):
        "hook a monitor to the base watcher"
        self._up.set()
        if cmd is None:
            return
        if self._link is None:
            return
        self._rd = aiter(cmd.base.watch(cmd.name, self._link))
        self._cmd = cmd
        try:
            await idle()
        finally:
            self._rd.close()

    async def read(self, t=100):
        """
        Wait for new data.
        If none arrive after 1/10th second, poll the remote side.

        If this method is called before "run", it waits for that to start.
        """
        if self._rd is not None:
            await self._up.wait()
            del self._up

        try:
            return await wait_for_ms(t, anext, self._rd)
        except TimeoutError:
            await self._cmd.send("sq", o=(self._cmd.name, self._link))

    async def send(self, msg):
        """
        don't send: we received it!
        """
        pass  # pylint:disable=unnecessary-pass
