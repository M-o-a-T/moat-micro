import sys
from functools import partial

from moat.util import NoProxyError, NotGiven, as_proxy, name2obj, obj2name
from moat.util.compat import Lock, TimeoutError, wait_for_ms

try:
    from moat.util import Proxy, get_proxy
except ImportError:
    Proxy = None

    def get_proxy(x):
        raise NotImplementedError(f"get_proxy({repr(x)})")


try:
    import greenback
except ImportError:
    greenback = None
from msgpack import ExtType, OutOfData, Packer, Unpacker, packb, unpackb
from serialpacker import FRAME_START, SerialPacker

from .stack import _Stacked

as_proxy("-", replace=True)(NotGiven)


if sys.implementation.name != "micropython":

    class SyncReadStream:
        """
        Convert an async stream to sync, via greenback. Use case: msgpack
        is not sans-IO and on regular Python we don't want to mangle
        msgpack to async-ize it.

        Writing is not implemented.
        """

        def __init__(self, stream):
            self.s = stream

        def read(self, n):
            return greenback.await_(self.s.recv(n))


class _Base(_Stacked):
    def __init__(self, stream):
        super().__init__(None)
        if sys.implementation.name == "micropython" and isinstance(stream, AIOStream):
            raise RuntimeError("ugh")
        self.s = stream

    async def aclose(self):
        self.s.close()


# msgpack encode/decode


def _decode(code, data):
    # decode an object, possibly by building a proxy.

    if code == 4:
        n = data.decode("utf-8")
        try:
            return name2obj(n)
        except KeyError:
            if Proxy is None:
                raise NoProxyError(n)
            return Proxy(n)
    elif code == 5:
        try:
            s = Unpacker(None)
            s.feed(data)

            s, *d = list(s)
            st = d[1] if len(d) > 1 else {}
            d = d[0]
            p = name2obj(s)
            try:
                o = p(*d, **st)
            except TypeError:
                o = p(*d)
                try:
                    o.__setstate__(st)
                except AttributeError:
                    try:
                        o.__dict__.update(st)
                    except AttributeError:
                        for k, v in st.items():
                            setattr(o, k, v)
            return o
        except Exception as exc:
            print("Cannot unpack", repr(data), file=sys.stderr)
            # fall thru to ExtType
    return ExtType(code, data)


def _encode(obj):
    # encode an object by building a proxy.

    if Proxy is not None and isinstance(obj, Proxy):
        return ExtType(4, obj.name.encode("utf-8"))

    try:
        k = obj2name(obj)
        return ExtType(4, k.encode("utf-8"))
    except KeyError:
        pass
    try:
        k = obj2name(type(obj))
    except KeyError:
        k = get_proxy(obj)
        return ExtType(4, k.encode("utf-8"))
    else:
        try:
            p = obj.__reduce__
        except AttributeError:
            try:
                p = obj.__dict__
            except AttributeError:
                p = {}
                for n in dir(obj):
                    if n.startswith("_"):
                        continue
                    p[n] = getattr(obj, n)
            p = ((), p)
        else:
            p = p()
            if hasattr(p[0], "__name__"):  # grah
                if p[0].__name__ == "_reconstructor":
                    p = (p[1][0], ()) + tuple(p[2:])
                elif p[0].__name__ == "__newobj__":
                    p = (p[1][0], p[1][1:]) + tuple(p[2:])

            assert p[0] is type(obj), (obj, p)
            p = p[1:]
        return ExtType(5, packb(k) + b"".join(packb(x) for x in p))


class MsgpackStream(_Stacked):
    """
    structured messages > MsgPack bytestream

    Use this if your stream is reliable (TCP, USB, …) but doesn't support
    message boundaries.
    """

    def __init__(self, stream, msg_prefix=None, console_handler=None, **kw):
        #
        # console_handler: called with console bytes
        # msg_prefix: int: code for start-of-packet
        #
        super().__init__(stream)
        self.w_lock = Lock()
        kw['ext_hook'] = _decode

        if console_handler is not None or msg_prefix is not None:
            kw["read_size"] = 1

        if sys.implementation.name == "micropython":
            # we use a hacked version of msgpack with a stream-y async unpacker
            self.pack = Packer(default=_encode).packb
            self.unpack = Unpacker(stream, **kw).unpack
        else:
            # regular Python: msgpack uses a sync read call, so use greenback to async-ize it
            self.pack = Packer(default=_encode).pack
            self.unpacker = Unpacker(SyncReadStream(stream), **kw)

            async def unpack():
                # This calls the unpacker synchronously, but reads from the
                # async stream via greenback
                import anyio

                try:
                    return self.unpacker.unpack()
                except OutOfData:
                    raise anyio.EndOfStream

            self.unpack = unpack
        self.msg_prefix = msg_prefix
        self.console_handler = console_handler

    async def send(self, msg):
        msg = self.pack(msg)
        async with self.w_lock:
            if self.msg_prefix is not None:
                await super().send(bytes((self.msg_prefix,)))
            await super().send(msg)

    async def recv(self):
        if self.msg_prefix is not None:
            while True:
                b = (await super().recv(1))[0]
                if b == self.msg_prefix:
                    res = await self.unpack()
                    return res
                if self.console_handler is not None:
                    self.console_handler(b)

        else:
            while True:
                try:
                    r = await self.unpack()
                except OutOfData:
                    raise EOFError
                if self.console_handler is not None and isinstance(r, int) and 0 <= r < 128:
                    self.console_handler(r)
                else:
                    return r


class MsgpackHandler(_Stacked):
    """
    structured messages > chunked bytestrings

    Use this if the layer below supports byte boundaries
    (one bytestring-ized message per call).
    """

    def __init__(self, stream, **kw):
        super().__init__(stream)
        try:
            self.unpacker = Unpacker(None, ext_hook=_decode, **kw).unpackb
            self.pack = Packer(default=_encode).packb
        except AttributeError:
            # SIGH
            self.unpacker = partial(unpackb, ext_hook=_decode, **kw)
            self.pack = partial(packb, default=_encode)

    async def send(self, msg):
        await super().send(self.pack(msg))

    async def recv(self):
        m = await super().recv()
        return self.unpacker(m)


class SerialPackerStream(_Base):
    """
    chunked bytestrings > SerialPacker-ized stream

    Use this (and a MsgpackHandler and a Reliable) if your AIO stream
    is unreliable (TTL serial).
    """

    def __init__(self, stream, console_handler=None, msg_prefix=FRAME_START, **kw):
        super().__init__(None)

        self.s = stream
        self.p = SerialPacker(frame_start=msg_prefix, **kw)
        self.buf = bytearray(16)
        self.i = 0
        self.n = 0
        self.console_handler = console_handler
        self.w_lock = Lock()

    async def recv(self):
        while True:
            while self.i < self.n:
                msg = self.p.feed(self.buf[self.i])
                self.i += 1
                if isinstance(msg, int):
                    if self.console_handler is not None:
                        self.console_handler(msg)
                elif msg is not None:
                    return msg

            n = await self.s.recvi(self.buf)
            if not n:
                raise EOFError
            self.i = 0
            self.n = n

    async def send(self, msg):
        h, msg, t = self.p.frame(msg)
        async with self.w_lock:
            await self.s.send(h)
            await self.s.send(msg)
            await self.s.send(t)

    async def aclose(self):
        await self.s.aclose()


try:
    from uasyncio import core
    from uasyncio.stream import Stream
except ImportError:
    pass
else:

    def _rdq(s):  # async
        yield core._io_queue.queue_read(s)

    def _wrq(s):  # async
        yield core._io_queue.queue_write(s)

    class AsyncStream(_Base):
        """
        adapt a sync MicroPython stream to MoaT

        reads a byte at a time if the stream doesn't have an "any()" method

        times out short reads if no more data arrives
        """

        _buf = None

        def __init__(self, s, sw=None, force_write=False):
            super().__init__(s)

            self._any = getattr(s, "any", lambda: 1)
            self._wlock = Lock()
            self.sw = sw or s  # write stream
            self.force_write = force_write

        async def recvi(self, buf, timeout=100):
            n = 0
            m = memoryview(buf)
            while len(m):
                if n == 0 or timeout < 0:
                    await _rdq(self.s)
                else:
                    try:
                        await wait_for_ms(timeout, _rdq, self.s)
                    except TimeoutError:
                        break
                d = self.s.readinto(m[: min(self._any(), len(m))])
                if not d:
                    break
                m = m[d:]
                n += d
            return n

        async def recv(self, n=128, timeout=100):
            buf = bytearray(n)
            i = await self.recvi(buf, timeout)
            if i < n:
                return buf[0:i]
            return buf

        async def send(self, buf):
            async with self._wlock:
                m = memoryview(buf)
                i = 0
                while i < len(buf):
                    if not self.force_write:  # XXX *sigh*
                        await _wrq(self.sw)
                    n = self.sw.write(m[i:])
                    if n:
                        i += n
                self._buf = None
                return i

    class AIOStream(_Base):
        # adapts an asyncio stream to ours
        def __init__(self, stream):
            self.s = stream.s
            self.send = stream.awrite
            self.recvi = stream.readinto
            self.aclose = stream.aclose

        async def recv(self, n=128):
            s = self.s
            buf = bytearray(n)
            res = await self.recvi(buf)
            if not res:
                raise EOFError
            if res == n:
                return buf
            elif res <= n / 4:
                return buf[:res]
            else:
                return memoryview(buf)[:res]
