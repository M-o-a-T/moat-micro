"""
Code to set up a link to a MicroPython client device
"""
import hashlib
import io
import logging
from contextlib import asynccontextmanager
from itertools import chain
from pathlib import Path

import anyio
from anyio_serial import Serial
from moat.util import NotGiven, attrdict
from moat.util.compat import AnyioMoatStream, Event, TaskGroup

from .cmd import BaseCmd
from .cmd import Request as BaseRequest
from .proto.console import console_stack

logger = logging.getLogger(__name__)


class ClientBaseCmd(BaseCmd):
    """
    a BaseCmd subclass that adds link state tracking
    """

    def __init__(self, parent, *, cfg=None):
        super().__init__(parent)
        self.cfg = cfg
        self.started = Event()

    def cmd_link(self, s=None):  # pylint: disable=unused-argument
        """Link-up command handler, sets `started`"""
        self.started.set()

    async def wait_start(self):
        """Wait until a "link" command arrives"""
        await self.started.wait()


class ABytes(io.BytesIO):
    """
    An async-IO-mimicing version of `io.BytesIO`.
    """

    def __init__(self, name, data):
        super().__init__()
        self.name = name
        self.write(data)
        self.suffix = Path(name).suffix

    def __str__(self):
        return str(self.name)

    async def open(self, mode):  # pylint: disable=unused-argument
        "reset the buffer pointer"
        self.seek(0, 0)
        return self

    async def read_bytes(self):
        "return the current buffer"
        return self.getbuffer()

    async def sha256(self):
        "hash the current buffer"
        _h = hashlib.sha256()
        _h.update(self.getbuffer())
        return _h.digest()

    def close(self):
        "does nothing"
        pass  # pylint:disable=unnecessary-pass

    async def is_dir(self):
        "returns False"
        return False

    async def is_file(self):
        "returns True"
        return True

    async def stat(self):
        "returns the size. All other stat fields are empty"
        res = attrdict()
        res.st_size = len(self.getbuffer())
        return res


class NoPort(RuntimeError):
    "Config error: no port given"
    pass  # pylint:disable=unnecessary-pass


async def copy_over(src, dst, cross=None):
    """
    Transfer a file tree from @src to @dst.

    This procedure verifies that the data arrived OK.
    """
    from moat.micro.path import copytree  # pylint:disable=import-outside-toplevel

    tn = 0
    if await src.is_file():
        if await dst.is_dir():
            dst /= src.name
    while n := await copytree(src, dst, cross=cross):
        tn += n
        if n == 1:
            logger.info("One file changed. Verifying.")
        else:
            logger.info("%d files changed. Verifying.", n)
    logger.info("Done. No (more) differences detected.")
    return tn


@asynccontextmanager
async def get_serial(obj, reset: bool = False, flush: bool = True):
    """\
        Context: the specified serial port, as an an AnyIO stream.

        This code clears RTS and DTR.
        """
    if not obj.port:
        raise NoPort("No port given")
    _h = {}
    try:
        _h['baudrate'] = obj.baudrate
    except AttributeError:
        pass
    # if not reset:
    #     _h["rts"] = False
    #     _h["dtr"] = False
    ser = Serial(obj.port, **_h)
    async with ser:
        # clear DTR+RTS. May reset the target.
        if reset:
            ser.rts = True
            ser.dtr = False
            await anyio.sleep(0.1)
            ser.rts = False

        # flush messages
        if flush:
            while True:
                with anyio.move_on_after(0.2):
                    res = await ser.receive(200)
                    logger.debug("Flush: %r", res)
                    continue
                break
        yield ser


@asynccontextmanager
async def get_link_serial(obj, ser, **kw):
    """\
        Context: Link to the target using the serial port @ser and a
        console-ish stack.

        Returns the top stream.
        """
    kw.setdefault("log", obj.debug > 2)
    kw.setdefault("lossy", obj.lossy)
    kw.setdefault("request_factory", Request)

    t, b = await console_stack(
        AnyioMoatStream(ser), msg_prefix=0xC1 if obj.guarded else None, **kw
    )

    async with TaskGroup() as tg:
        task = await tg.spawn(b.run, _name="ser")
        try:
            yield t
        finally:
            task.cancel()


@asynccontextmanager
async def get_link(obj, *, use_port=False, reset=False, cfg=None, **kw):
    """\
        Context: Link to the target: the Unix-domain socket, if that can be
        connected to, or the serial port.

        Returns the top MoaT stream.
        """
    kw.setdefault("log", obj.debug > 2)
    kw.setdefault("lossy", False)
    kw.setdefault("request_factory", Request)

    try:
        if obj.socket:
            sock = await anyio.connect_unix(obj.socket)
        else:
            raise AttributeError("socket")
    except (AttributeError, OSError):
        if not use_port:
            raise
        async with get_serial(obj, reset=reset, flush=True) as ser:
            async with get_link_serial(obj, ser, **kw) as link:
                yield link
    else:
        try:
            t, b = await console_stack(AnyioMoatStream(sock), **kw)
            t = t.stack(ClientBaseCmd, cfg=cfg)
            async with TaskGroup() as tg:
                task = await tg.spawn(b.run, _name="link")
                yield t.request
                task.cancel()
        finally:
            await sock.aclose()


@asynccontextmanager
async def get_remote(obj, host, port=27587, **kw):
    """\
        Context: Link to a network target: host+port

        Returns the top MoaT stream.
        """
    async with await anyio.connect_tcp(host, port) as sock:
        try:
            t, b = await console_stack(
                AnyioMoatStream(sock),
                request_factory=Request,
                log=obj.debug > 2,
                lossy=False,
                **kw,
            )
            async with TaskGroup() as tg:
                task = await tg.spawn(b.run, _name="rem")
                yield t
                task.cancel()
        finally:
            await sock.aclose()


class Request(BaseRequest):
    """
    "Main" Request class.

    Also handles retrieving/setting the configuration from/to the client.
    """

    #   def __init__(self, *a, cmd_cls=ClientBaseCmd, cfg=None, **k):
    #       super().__init__(*a, **k)
    #       if cfg is None:
    #           raise TypeError("cfg")
    #       self.stack(cmd_cls, cfg=cfg)

    APP = None

    async def get_cfg(self):
        """
        Collect the client's configuration data.
        """

        async def _get_cfg(p):
            d = await self.send(("sys", "cfg_r"), p=p)
            if isinstance(d, (list, tuple)):
                d, s = d
                if isinstance(d, dict):
                    d = attrdict(d)
                for k in s:
                    d[k] = await _get_cfg(p + (k,))
            return d

        cfg = await _get_cfg(())
        self.base.cfg = cfg
        return cfg

    async def set_cfg(self, cfg, replace=False, sync=False):
        """
        Update the client's configuration data.

        If @replace is set, the config file is complete and any other items
        will be deleted from the client.

        If @sync is set, the client will reload apps etc. after updating
        the config.
        """

        async def _set_cfg(p, c):
            # current client cfg
            try:
                ocd, ocl = await self.send(("sys", "cfg_r"), p=p)
            except KeyError:
                ocd = {}
                ocl = []
                await self.send(("sys", "cfg"), p=p, d={})
            for k, v in c.items():
                if isinstance(v, dict):
                    await _set_cfg(p + (k,), v)
                elif ocd.get(k, NotGiven) != v:
                    await self.send(("sys", "cfg"), p=p + (k,), d=v)

            if not replace:
                return
            # drop those client cfg snippets that are not on the server
            for k in chain(ocd.keys(), ocl):
                if k not in c:
                    await self.send(("sys", "cfg"), p=p + (k,), d=NotGiven)

        await _set_cfg((), cfg)
        if sync:
            await self.send(("sys", "cfg_x"))  # runs
