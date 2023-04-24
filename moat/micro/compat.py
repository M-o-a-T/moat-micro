"""
Compatibility wrappers that allows MoaT code to run on CPython/anyio as
well as MicroPython/uasyncio.

Well, for the most part.
"""
import anyio as _anyio

class AnyioMoatStream:
    """
    Adapts an anyio stream to MoaT
    """

    def __init__(self, stream):
        self.s = stream
        self.aclose = stream.aclose

    async def recv(self, n=128):
        "basic receive"
        try:
            res = await self.s.receive(n)
            return res
        except (_anyio.EndOfStream, _anyio.ClosedResourceError):
            raise EOFError from None

    async def send(self, buf):
        "basic send"
        try:
            return await self.s.send(buf)
        except (_anyio.EndOfStream, _anyio.ClosedResourceError):
            raise EOFError from None

    async def recvi(self, buf):
        "basic receive into"
        try:
            res = await self.s.receive(len(buf))
        except (_anyio.EndOfStream, _anyio.ClosedResourceError):
            raise EOFError from None
        else:
            buf[: len(res)] = res
            return len(res)
