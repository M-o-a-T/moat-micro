"""
Basic handler for iterating incoming Moat connections.
"""

from __future__ import annotations

from moat.util import Queue
from moat.micro.compat import ACM, AC_exit, Event, TaskGroup, L

# typing
from typing import TYPE_CHECKING  # isort:skip

if TYPE_CHECKING:
    from typing import Awaitable, Never

    from moat.micro.proto.stack import BaseConn


TEST_MAGIC = b"r\x0dn\x0a-\x00x\x0ce\x1b!"


class BaseConnIter:
    """
    Iterate incoming connections.

    You need to override the "accept" method.
    """

    def __init__(self):
        self.q = Queue(1)
        self.evt = Event()

    async def __aenter__(self):
        self.tg = await ACM(self)(TaskGroup())
        await self.tg.spawn(self.accept)
        return self

    async def __aexit__(self, *exc):
        await AC_exit(self, *exc)

    if L:
        def set_ready(self) -> None:
            "signals that the socket-or-whatever accepts connections"
            self.evt.set()

        def is_ready(self) -> Awaitable:
            "wait for the socket-or-whatever to accept connections"
            return self.evt.wait()

    def add_conn(self, c: BaseConn) -> Awaitable:
        "queues the connection for starting a task"
        return self.q.put(c)

    async def accept(self) -> Never:
        """
        Background task to accept incoming connections.

        Call ``await self.add_conn(conn)`` for each connection.

        Call ``self.ready()`` as soon as the socket-or-whatever is ready to accept links.
        """
        raise NotImplementedError

    def __aiter__(self):
        return self

    def __anext__(self) -> Awaitable[BaseConn]:
        return self.q.get()
