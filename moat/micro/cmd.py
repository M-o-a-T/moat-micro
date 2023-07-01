"""
Server side of BaseCmd

extends BaseCmd to also return loc_* functions
"""

from moat.util.broadcast import Broadcaster

from ._cmd import BaseCmd as _BaseCmd
from ._cmd import RootCmd as _RootCmd
from ._cmd import Request  # pylint:disable=unused-import

class BaseCmd(_BaseCmd):
    """
    The server-side BaseCmd class also returns a list of local commands in
    `_dir`.
    """

    def cmd__dir(self):
        """
        Rudimentary introspection. Returns a list of available commands @c,
        submodules @d, and local commands @e.

        @j is True if there's a generic command handler.
        """
        e = []
        res = super().cmd__dir()
        for k in dir(self):
            if k.startswith("loc_") and k[4] != '_':
                e.append(k[4:])
        if e:
            res["e"] = e
        return res

    loc__dir = cmd__dir


class RootCmd(_RootCmd):
    """
    The server-side BaseCmd class maintains a broadcaster for local clients.
    """

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._mon = Broadcaster()

    async def monitor(self, qlen=10):
        """
        State update iterator. Yields (path,data) tuples.

        @qlen is the length of the broadcaster's queue.
        """
        async for msg in self._mon.reader(qlen):
            yield msg


    
