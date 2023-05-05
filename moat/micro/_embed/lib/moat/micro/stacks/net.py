import sys

from moat.util.compat import Event, TaskGroup, print_exc, run_server
from uasyncio.queues import Queue

from ..cmd import Request
from ..proto.stream import AIOStream, MsgpackStream


async def network_stack(
    callback, log=False, multiple=False, host="0.0.0.0", port=0, request_factory=Request
):
    # an iterator for network connections / their stacks. Yields one t,b
    # pair for each successful connection.
    #
    # If @multiple is False there can be only one connection.
    # the returned stack directly, the listener socket will be re-opened
    # when you re-enter the iterator.
    #
    # Otherwise there can be multiple connections, i.e. run the returned
    # stack in a taskgroup.
    #
    # "Yields" means that the callback is called, in a new task.
    # (PEP 525's async iterators have not been fully implemented in MicroPython.)

    if log:
        from .proto.stack import Logger
    q = Queue(0)

    async def make_stack(s, rs):
        assert s is rs
        await q.put(s)

    srv = None
    n = 0
    async with TaskGroup() as tg:
        await tg.spawn(run_server, make_stack, host, port, taskgroup=tg, _name="run_server")
        while True:
            s = await q.get()
            n += 1
            if srv is not None:
                srv.cancel()
            t = b = AIOStream(s)
            t = t.stack(MsgpackStream)
            if log:
                t = t.stack(Logger, txt="N%d" % n)
            t = t.stack(request_factory)
            srv = await tg.spawn(callback, t, b, _name="ns_cb")
