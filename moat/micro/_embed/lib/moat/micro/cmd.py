"""
Basic infrastructure to run an RPC system via an unreliable,
possibly-reordering, and/or stream-based transport

We have a stack of classes, linked by parent/child pointers.
At the bottom there's some Stream adapter. At the top we have the command
handling, implemented by the classes Request (send a command, wait for
reply) and Base (receive a command, generate a reply). Base classes form
a tree.

Everything is fully asynchronous. Each class has a "run" method which is
required to call its child's "run", as well as do internal housekeeping
if required. A "run" method may expect its parent to be operational;
it gets cancelled if/when that is no longer true. When a child's "run"
terminates, the parent's "run" needs to return.

Incoming messages are handled by the child's "dispatch" method. They
are expected to be fully asynchronous, i.e. a "run" method that calls
"dispatch" must use a separate task to do so.

Outgoing messages are handled by the parent's "send" method. Send calls
return when the data has been sent, implying that sending on an
unreliable transport will wait for the message to be confirmed. Sending
may fail.
"""


import sys

from moat.util import (  # pylint: disable=no-name-in-module
    Broadcaster,
    Queue,
    ValueEvent,
    as_proxy,
    attrdict,
    import_,
    obj2name,
)
from moat.util.compat import CancelledError, Event, TaskGroup, WouldBlock, idle, print_exc

from moat.micro.proto.stack import RemoteError, SilentRemoteError, _Stacked

as_proxy("_KyErr", KeyError, replace=True)
as_proxy("_AtErr", AttributeError, replace=True)


class BaseCmd(_Stacked):
    """
    Request/response handler

    This is attached as a child to the Request object.

    Incoming requests call `cmd_*` with `*` being the action. If the
    action is a string, the complete string is tried first, then
    the first character. Otherwise (action is a list) the first
    element is used as-is.

    If the action is empty, call the `cmd` method instead. Otherwise if
    no method is found return an error.

    Attach a sub-base directly to their parents by setting their
    `cmd_XX` property to it.

    The `send` method simply forwards to its parent, for convenience.

    This is the toplevel entry point. You build a request stack by piling
    modules on top of each other; the final one is a Request. On top of
    that Request you stack Base subclasses according to the functions you
    need.
    """

    _tg: TaskGroup = None

    async def run(self):
        """
        Main loop for this part.

        By default, does nothing.
        """
        pass  # pylint: disable=unnecessary-pass

    async def run_sub(self):
        """
        Runs my (and my children's) "run" methods.
        """
        async with TaskGroup() as tg:
            self._tg = tg
            await tg.spawn(self.run, _name="run")

            for k in dir(self):
                if not k.startswith('dis_'):
                    continue
                v = getattr(self, k)
                if isinstance(v, BaseCmd):
                    await tg.spawn(v.run_sub, _name="sub:" + k)

    async def aclose(self):
        """
        Stop my (and my children's) "run" methods.
        """
        self._tg.cancel()
        await super().aclose()

    async def dispatch(
        self, action: str | list[str], msg: dict
    ):  # pylint:disable=arguments-differ
        # TODO rename dispatch+send when with actions
        """
        Process one incoming message.

        @msg is either a dict (keyword+value for the destination handler)
        or not (single direct argument).

        @action may be a string or an array. The first element of
        the array is used to look up a submodule. Same for the first char
        of a string, if there's no command with that name. An empty-string
        action calls the ``cmd`` method.

        Returns whatever the called command returns/raises, or raises
        AttributeError if no command is found.
        """

        async def c(p):
            if isinstance(msg, dict):
                r = p(**msg)
            else:
                r = p(msg)
            if hasattr(r, "throw"):  # coroutine
                r = await r
            return r

        if not action:
            # pylint: disable=no-member
            return await c(self.cmd)
            # if there's no "self.cmd", the resulting AttributeError is our reply

        if isinstance(action, str) and len(action) > 1:
            try:
                p = getattr(self, "cmd_" + action)
            except AttributeError:
                pass
            else:
                return await c(p)

        if len(action) > 1:
            try:
                dis = getattr(self, "dis_" + action[0])
            except AttributeError:
                raise AttributeError(action) from None
            else:
                return await dis(action[1:], msg)
        else:
            return await c(getattr(self, "cmd_" + action[0]))

    async def __call__(self, *a, **k):
        """
        Alias for the dispatcher
        """
        return await self.dispatch(*a, **k)

    async def config_updated(self, cfg):
        """
        Trigger: when the config has been updated, tell this module to
        update itself.

        May be overridden, but do call ``super()``.
        """
        for k in dir(self):
            if k.startswith("dis_"):
                v = getattr(self, k)
                await v.config_updated(cfg.get(k[4:], {}))

    def cmd__dir(self):
        """
        Rudimentary introspection. Returns a list of available commands @c and
        submodules @d
        """
        d = []
        c = []
        res = dict(c=c, d=d)
        for k in dir(self):
            if k.startswith("cmd_") and k[4] != '_':
                c.append(k[4:])
            elif k.startswith("dis_") and k[4] != '_':
                d.append(k[4:])
            elif k == "cmd":
                res['j'] = True
        return res

    @property
    def request(self):
        "returns the request handler. Just asks the parent."
        return self.parent.request

    @property
    def base(self):
        "returns base command."
        return self.parent.base


class Request(_Stacked):
    """
    Request/Response handler (client side)

    Call "send" with an action (a string or list) to select
    the function of the recipient. The response is returned / raised.
    The second argument is expanded by the recipient if it is a dict.
    Requests are cancelled when the lower layer terminates.

    The transport may re-order messages, but it must not lose them.

    This is the "top" module of a connection's stack.

    @ready is an Enevt that'll be set when the system is up.
    """

    APP = "app"
    _tg: TaskGroup = None

    def __init__(self, *a, ready=None, **k):
        if sys.implementation.name != "micropython" and self.APP == "app":
            raise ValueError("Cannot work")
        super().__init__(*a, **k)
        self.reply = {}
        self.seq = 0
        self._ready = ready
        self.apps = {}

    @property
    def request(self):
        "return request handler, i.e. self"
        return self

    @property
    def base(self):
        "return base command, i.e. my child"
        return self.child

    async def wait_ready(self):
        "delay until ready"
        if self._ready is not None:
            await self._ready.wait()

    async def update_config(self):
        "called after the config has been updated"
        if self.APP is not None:
            await self._setup_apps()

    async def _setup_apps(self):
        # TODO send errors back
        if self.APP is None:
            return
        gcfg = self.base.cfg
        apps = gcfg.get("apps", {})
        tg = self._tg

        def imp(name):
            return import_(f"{self.APP}.{name}", 1)

        for name in list(self.apps.keys()):
            if name not in apps:
                app = self.apps.pop(name)
                delattr(self.base, "dis_" + name)
                app._req_scope.cancel()  # pylint: disable=protected-access
                sys.modules.pop(app.__module__, None)

        # First setup the app data structures
        for name, v in apps.items():
            if name in self.apps:
                continue

            cfg = getattr(gcfg, name, {})
            cmd = imp(v)(self, name, cfg, gcfg)
            self.apps[name] = cmd
            setattr(self.base, "dis_" + name, cmd)

        # then run them all.
        # For existing apps, tell it to update its configuration.
        for name, app in self.apps.items():
            if hasattr(app, "_req_scope"):
                cfg = getattr(gcfg, name, attrdict())
                await app.config_updated(cfg)
            else:
                app._req_scope = await tg.spawn(  # pylint: disable=protected-access
                    app.run, _name="mp_app_" + name
                )

        if self._ready is not None:
            self._ready.set()

    async def run(self):
        """
        Main loop for this stack. Starts child modules' mainloops and
        reads+dispatches incoming requests.
        """
        try:
            async with TaskGroup() as tg:
                self._tg = tg
                await tg.spawn(self._setup_apps)

                while True:
                    msg = await self.parent.recv()
                    await self.dispatch(msg)
        finally:
            self._cleanup_open_commands()
            await self.aclose()

    def _cleanup_open_commands(self):
        for e in self.reply.values():
            e.set_error(CancelledError())

    async def _handle_request(self, a, i, d, msg):
        """
        Handler for a single request.

        `dispatch` starts this in a new task.
        """
        res = {'i': i}
        try:
            r = await self.child.dispatch(a, d)
        except SilentRemoteError as exc:
            if i is None:
                return
            res["e"] = exc
        except WouldBlock:
            raise
        except Exception as exc:  # pylint:disable=broad-exception-caught
            # TODO only when debugging
            print("ERROR handling", a, i, d, msg, file=sys.stderr)
            print_exc(exc)
            if i is None:
                return
            try:
                obj2name(type(exc))
            except KeyError:
                res["e"] = "E:" + repr(exc)
            else:
                res["e"] = exc
        else:
            if i is None:
                return
            res["d"] = r
        try:
            await self.parent.send(res)
        except TypeError as exc:
            print("ERROR returning", res, file=sys.stderr)
            print_exc(exc)
            res = {'e': "T:" + repr(exc), 'i': i}
            await self.parent.send(res)

    async def dispatch(self, msg):  # pylint:disable=arguments-differ
        """
        Main handler for incoming messages
        """
        if not isinstance(msg, dict):
            print("?3", msg, file=sys.stderr)
            return
        a = msg.pop("a", None)
        i = msg.pop("i", None)
        d = msg.pop("d", None)

        if a is not None:
            # request from the other side
            # runs in a separate task
            # TODO create a task pool
            await self._tg.spawn(self._handle_request, a, i, d, msg, _name="hdl:" + str(a))

        else:
            # reply
            if i is None:
                # No seq#. Dunno what to do about these.
                print("?4", d, msg, file=sys.stderr)
                return

            e = msg.pop("e", None) if d is None else None
            try:
                evt = self.reply[i]
            except KeyError:
                print("?5", i, msg, file=sys.stderr)
                return  # errored?
            if evt.is_set():
                print("Duplicate reply?", a, i, d, msg, file=sys.stderr)
                return  # duplicate??
            if e is None:
                evt.set(d)
            elif isinstance(e, Exception):
                evt.set_error(e)
            else:
                evt.set_error(RemoteError(e, d))

    async def send(self, action, _msg=None, **kw):  # pylint:disable=arguments-differ
        """
        Send a request, return the response.

        The message is either the second parameter, or a dict (use any
        number of keywords).
        """
        if _msg is None:
            _msg = kw
        elif kw:
            raise TypeError("cannot use both msg data and keywords")

        # Find a small-ish but unique seqnum
        if self.seq > 10 * (len(self.reply) + 5):
            self.seq = 9
        while True:
            self.seq += 1
            seq = self.seq
            if seq not in self.reply:
                break
        msg = {"a": action, "d": _msg, "i": seq}

        self.reply[seq] = e = ValueEvent()
        try:
            await self.parent.send(msg)
            return await e.get()
        finally:
            del self.reply[seq]

    async def send_nr(self, action, msg=None, **kw):
        """
        Send an unsolicited message (no seqnum == no reply)
        """
        if msg is None:
            msg = kw
        elif kw:
            raise TypeError("cannot use both msg data and keywords")

        msg = {"a": action, "d": msg}
        await self.parent.send(msg)


class RootCmd(BaseCmd):
    """
    Standard toplevel base implementation.

    Adds commands to set and query broadcasters.
    """

    _cfg = None

    def __init__(self, parent):
        super().__init__(parent)
        self._s = {}
        self._q = Queue(20)

    #   @property
    #   def cfg(self):
    #       return self._cfg
    #   @cfg.setter
    #   def cfg(self, val):
    #       if not isinstance(val, attrdict):
    #           breakpoint()
    #       self._cfg = val

    async def cmd_s(self, o, d):
        """
        State update.
        """
        b = self._get_br(o)
        b(d)

    async def cmd_sq(self, o):
        """
        State query.
        """
        b = self._get_br(o)
        return await b.read()

    loc_s = cmd_s
    loc_sq = cmd_sq

    async def run(self):
        try:
            await idle()
        finally:

            def _cls(s):
                for v in s.values():
                    if isinstance(v, dict):
                        _cls(v)
                    else:
                        v.close()

            _cls(self._s)

    def _get_br(self, o):
        s = self._s
        for x in o[:-1]:
            try:
                s = s[x]
            except KeyError:
                s = s[x] = {}
        x = o[-1]
        try:
            b = s[x]
        except KeyError:
            b = s[x] = RemBroadcaster()
            Broadcaster.__enter__(b)  # pylint:disable=unnecessary-dunder-call
        return b

    def watch(self, *o):
        "return a context to watch a specific broadcast"
        b = self._get_br(o)
        return b.reader(1)

    def register(self, obj, *o):
        "register a source object for a specific broadcast"
        b = self._get_br(o)
        if b.obj is not None:
            raise RuntimeError(f"{b.obj} already registered on {o}")
        b.obj = obj

        # pylint:disable=protected-access
        if b._evt is not None:
            b._evt.set()
            b._evt = None
        return b


class RemBroadcaster(Broadcaster):
    "A broadcaster that reads from an object"
    obj = None
    _evt = None

    def __repr__(self):
        return f"{super().__repr__()} o={repr(self.obj)} e={'Y' if self._evt else 'N'}"

    def __enter__(self):
        return self

    def __exit__(self, *tb):
        self.obj = None

    async def read(self):
        "read data"
        if self.obj is None:
            if self._evt is None:
                self._evt = Event()
            await self._evt.wait()
        return await self.obj.read_()
