import logging
import sys

import anyio
import asyncdbus.service as _dbus
from asyncdbus.message_bus import BusType, MessageBus
from moat.lib.victron.dbus import Dbus
from moat.util import Queue, attrdict

from moat.micro.app import BaseAppCmd
from moat.util.compat import Event, TaskGroup, sleep_ms, ticks_add, ticks_diff, ticks_ms

from .. import BaseAppCmd

logger = logging.getLogger(__name__)

# cfg:
#   batt:
# u:
#   pin: PIN  # measure U
#   min: VAL
#   max: VAL
# i:
#   pin: PIN  # measure I
#   ref: PIN  # I reference, subtract from measurement
#   min: VAL
#   max: VAL
# poll:
#   t: MSEC
#   d: FACTOR # decay, for averaging, 1000/th
# rel: PIN  # relay
#


class NoSuchCell(RuntimeError):
    pass


class SpuriousData(RuntimeError):
    pass


class MessageLost(RuntimeError):
    pass


class BMSCmd(BaseAppCmd):
    def __init__(self, *a, **k):
        # name cfg gcfg
        super().__init__(*a, **k)

        from .controller import Controller

        self.ctrl = Controller(self, self.name, self.cfg, self.gcfg)

    async def cmd_work(self, **data):
        logger.info("WORK %s", data)
        self.ctrl.batt[0].add_work(
            data["w"] / (1000 / self.cfg.poll.t), data["n"] / (1000 / self.cfg.poll.t)
        )
        # XXX which battery?

    # 	async def loc_data(self):
    # 		# return "global" BMS data
    # 		await self.batt.updated.wait()
    # 		return self.batt.data

    async def config_updated(self, cfg):
        await self.ctrl.config_updated()

    async def run(self):
        async with MessageBus(bus_type=BusType.SYSTEM).connect() as bus:
            # 			async with TaskGroup() as tg:
            # 				await tg.spawn(self.ctrl.run, bus)
            await self.ctrl.run(bus)
