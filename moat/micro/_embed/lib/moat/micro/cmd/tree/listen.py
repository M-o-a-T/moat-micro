"""
Command tree support for MoaT commands
"""

from __future__ import annotations

from functools import partial

from moat.util import Path, import_
from moat.micro.compat import AC_use, Event, TaskGroup, log

from moat.micro.cmd.base import ACM_h, BaseCmd, ShortCommandError
from .layer import BaseLayerCmd
from .dir import BaseSubCmd

# Typing

from typing import TYPE_CHECKING  # isort:skip

if TYPE_CHECKING:
    from typing import AsyncContextManager, Awaitable, Never

    from moat.micro.proto.stack import BaseBuf, BaseMsg
    from moat.micro.stacks.util import BaseConnIter


class BaseListenOneCmd(BaseLayerCmd):
    """
    An app that runs a listener and accepts a single connection.

    Override `listener` to return it.

    TODO: this needs to be a stream layer instead: we want the
    Reliable module to be able to pick up where it left off.
    """

    def listener(self) -> BaseConnIter:
        """
        How to get new connections. Returns a BaseConnIter.

        Must be implemented in your subclass.
        """
        raise NotImplementedError

    def wrapper(self, conn) -> BaseMsg:
        """
        How to wrap the connection so that you can communicate on it.

        By default, use `console_stack`.
        """
        # pylint:disable=import-outside-toplevel
        from moat.micro.stacks.console import console_stack

        return console_stack(conn, self.cfg)

    async def reject(self, conn: BaseBuf):
        """
        Close the connection.
        """
        # an async context should do it
        async with conn:
            pass

    async def handler(self, conn):
        """
        Process a connection
        """
        from moat.micro.cmd.stream.cmdmsg import ExtCmdMsg  # pylint:disable=import-outside-toplevel

        app = ExtCmdMsg(self.wrapper(conn), self.cfg)
        if (
            self.app is None
            or not self.app.is_ready()
            or self._running
            or self.cfg.get("replace", True)
        ):
            if self.app is not None:
                await self.app.stop()
            app.attached(self, "_")
            self.app = app
            await self.start_app(app)
            self.set_ready()
            await app.wait_ready()

            await app.wait_stopped()
            if self.app is app:
                self.app = None
        else:
            # close the thing
            await self.reject(conn)

    async def task(self) -> Never:
        """
        Accept connections.
        """
        async with self.listener() as conns:
            async for conn in conns:
                await self.tg.spawn(self.handler, conn)


class BaseListenCmd(BaseSubCmd):
    """
    An app that runs a listener and connects all incoming connections
    to numbered subcommands.

    Override `listener` to return an async context manager / iterator.
    """

    seq = 1

    # no multiple inheritance for MicroPython
    listener = BaseListenOneCmd.listener
    wrapper = BaseListenOneCmd.wrapper

    async def handler(self, conn):
        """
        Process a new connection.
        """
        from moat.micro.cmd.stream.cmdmsg import ExtCmdMsg  # pylint:disable=import-outside-toplevel

        conn = self.wrapper(conn)
        app = ExtCmdMsg(conn, self.cfg)
        seq = self.seq
        if seq > len(self.sub) * 3:
            seq = 10
        while seq in self.sub:
            seq += 1
        self.seq = seq + 1
        await self.attach(seq, app)
        await self.start_app(app)
        await app.wait_ready()

        await app.wait_stopped()
        await self.detach(seq)

    async def task(self) -> Never:
        """
        Accept connections.
        """
        async with self.listener() as conns:
            self.set_ready()
            async for conn in conns:
                await self.tg.spawn(self.handler, conn)
