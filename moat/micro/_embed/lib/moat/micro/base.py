import machine

from .cmd import BaseCmd
from .compat import TaskGroup, sleep_ms, ticks_ms, ticks_diff
from moat.util import NotGiven
from .proto.stack import RemoteError
from .proto.stream import drop_proxy

import machine
import uos
import usys
import gc

class NoArg:
    pass

def _complex(v):
    if not isinstance(v,(dict,list,tuple)):
        return False
    # TODO check length of packed object without actually packing it
    return True

class SysCmd(BaseCmd):
    # system and other low level stuff
    def __init__(self, parent):
        super().__init__(parent)
        self.repeats = {}

    async def cmd_is_up(self):
        """
        Trigger an unsolicited ``link`` message. The data will be ``True``
        obviously.
        """
        await self.request.send_nr("link",True)

    async def cmd_state(self, state=None):
        """
        Set/return the string in the MoaT state file.

        The result is a dict:
        * n: current file content
        * c: state when the system was booted
        * fb: flag whether the current state is a fall-back state
        """
        if state is not None:
            f=open("moat.state","w")
            f.write(state)
            f.close()
        else:
            try:
                f=open("moat.state","r")
            except OSError:
                state=None
            else:
                state = f.read()
                f.close()
        return dict(n=state, c=self.base.moat_state, fb=self.base.is_fallback)

    async def cmd_test(self):
        """
        Returns a test string: r CR n LF - NUL c ^C e ESC !

        Use this to verify that nothing mangles anything.
        """
        return b"r\x0dn\x0a-\x00x\x0ce\x1b!"

    async def cmd_wdt(self, t=None):
        """
        Starts the watchdog (@t > 0) or restarts it.
        """
        from .main import wdt
        w = wdt(t=t)
        if w:
            w.feed()  # XXX ignores T, might error instead
        elif t:
            wdt(machine.WDT(t))
        else:
            # no watchdog running
            return False
        return True

    async def cmd_cfg_r(self, fn:str=None):
        """
        Read the current configuration from this file.
        """
        b = self.base
        if fn is None:
            fb = b.is_fallback
            fn = "moat_fb.cfg" if fb else "moat.cfg"

        f = SFile(open(fn,"rb"))
        try:
            cfg = await Unpacker(f).unpack()
        finally:
            await f.aclose()

        b.cfg = cfg
        await b.config_updated()
        await self.request.send_nr(["mplex","cfg"])


    async def cmd_cfg(self, p=(), d=NoArg):
        """
        Online configuration mangling.

        As configuration data are frequently too large to transmit in one
        go, this code interrogates and updates it step-by-step.

        @p is the path. An empty path is the root. Destinations are
        autogenerated. A path element of -1 appends.

        @d is the data replacing the destination. ``NotGiven`` deletes.
        if @d is None, return data consists of a dict/list (simple keys and
        values) and a list (keys/offsets for complex values).

        As a special case, @p=@d=None tells the client to apply the new
        values you transmitted. Whether a subsystem requires this update
        depends on the subsystem.

        There is no way to write the current config to the file system.
        Do this from the server.
        """
        cur = self.base.cfg
        if d is NoArg:
            for pp in p:
                cur = cur[pp]
            if isinstance(cur,dict):
                c = {}
                s = []
                for k,v in cur.items():
                    if _complex(v):
                        s.append(k)
                    else:
                        c[k] = v
                return c,s
            elif isinstance(cur,(list,tuple)):
                c = []
                s = []
                for k,v in enumerate(cur):
                    if _complex(v):
                        c.append(None)
                        s.append(k)
                    else:
                        c.append(v)
                return c,s
            else:
                return cur
                # guaranteed not to be a tuple
        if not p:
            if d is not None:
                raise ValueError("no override root")
            await self.base.config_updated(self.base.cfg)
            await self.request.send_nr(["mplex","cfg"])
            return
        for pp in p[:-1]:
            try:
                cur = cur[pp]
            except KeyError:
                cur[pp] = []
                cur = cur[pp]
            except IndexError as exc:
                if len(cur) != pp:
                    raise exc
                cur.append({})
                cur = cur[pp]
        if d is NotGiven:
            del cur[p[-1]]
        else:
            try:
                cur[p[-1]] = d
            except IndexError:
                if len(cur) != p[-1]:
                    raise exc
                cur.append(d)


    async def cmd_eval(self, val, attrs=()):
        """
        Evaluates ``val`` if it's a string, accesses ``attrs``,
        then returns a ``val, repr(val)`` tuple.

        If you get a `Proxy` object as the result, you need to call
        ``sys.unproxy`` to clear it from the cache.
        """
        if isinstance(val,str):
            val = eval(val,dict(s=self.parent))
        # otherwise it's probably a proxy
        for vv in attrs:
            try:
                val = getattr(v,vv)
            except AttributeError:
                val = val[vv]
        return (val,repr(val))  # may send a proxy

    async def cmd_unproxy(self, p):
        """
        Tell the client to forget about a proxy.

        @p accepts either the proxy's name, or the proxied object.
        """
        if p == "" or p == "-" or p[0] == "_":
            raise RuntimeError("cannot be deleted")
        drop_proxy(p)

    async def cmd_dump(self, x):
        """
        Evaluate an object, returns a repr() of all its attributes.

        Warning: this may need a heap of memory.
        """
        if isinstance(x,str):
            x = eval(x,dict(s=self.parent))
        d = {}
        for k in dir(x):
            d[k] = repr(getattr(res,k))
        return d

    async def cmd_info(self):
        """
        Returns some basic system info.
        """
        d = {}
        fb = self.base.is_fallback
        if fb is not None:
            d["fallback"] = fb
        d["path"] = usys.path
        return d

    async def cmd_mem(self):
        """
        Info about memory. Calls `gc.collect`.

        * f: free memory
        * c: memory freed by the garbage collector
        * t: time (ms) for the garbage collector to run
        """
        t1 = ticks_ms()
        f1 = gc.mem_free()
        gc.collect()
        f2 = gc.mem_free()
        t2 = ticks_ms()
        return dict(t=ticks_diff(t2,t1), f=f2, c=f2-f1)

    async def cmd_boot(self, code):
        """
        Reboot the system (soft reset).

        @code needs to be "SysBooT".
        """
        if code != "SysBooT":
            raise RuntimeError("wrong")

        async def _boot():
            await sleep_ms(100)
            await self.request.send_nr("link",False)
            await sleep_ms(100)
            machine.soft_reset()
        await self.request._tg.spawn(_boot, _name="base.boot1")
        return True

    async def cmd_reset(self, code):
        """
        Reboot the system (hard reset).

        @code needs to be "SysRsT".
        """
        if code != "SysRsT":
            raise RuntimeError("wrong")
        async def _boot():
            await sleep_ms(100)
            await self.request.send_nr("link",False)
            await sleep_ms(100)
            machine.reset()
        await self.request._tg.spawn(_boot, _name="base.boot2")
        return True

    async def cmd_stop(self, code):
        """
        Terminate MoaT and go back to MicroPython.

        @code needs to be "SysStoP".
        """
        # terminate the MoaT stack w/o rebooting
        if code != "SysStoP":
            raise RuntimeError("wrong")
        async def _boot():
            await sleep_ms(100)
            await self.request.send_nr("link",False)
            await sleep_ms(100)
            raise SystemExit
        await self.request._tg.spawn(_boot, _name="base.boot3")
        return True

    async def cmd_load(self, n, m, r=False, kw={}):
        """
        (re)load a dispatcher: set dis_@n to point to @m.

        Set @r if you want to reload the module if it already exists.
        @kw may contain additional params for the module.

        For example, ``most micro cmd sys.load -v n f -v m fs.FsCmd``
        loads the file system module if it isn't loaded already.
        """
        om = getattr(self.parent,"dis_"+n, None)
        if om is not None:
            if not r:
                return
            await om.aclose()
            del om  # free memory

        from .main import import_app
        m = import_app(m, drop=True)
        m = m(self.parent, n, kw, self.base.cfg)
        setattr(self.parent,"dis_"+n, m)
        await self.parent._tg.spawn(m.run_sub, _name="base.load")

    async def cmd_machid(self):
        """
        Return the machine's unique ID. This is the bytearray returned by
        `micropython.unique_id`.
        """
        return machine.unique_id()

    async def cmd_rtc(self, d=None):
        """
        Set, or query, the current time.
        """
        if d is None:
            return machine.RTC.now()
        else:
            machine.RTC((d[0],d[1],d[2],0, d[3],d[4],d[5],0))

    async def cmd_pin(self, n, v=None, **kw):
        """
        Set or read a digital pin.
        """
        p=machine.Pin(n, **kw)
        if v is not None:
            p.value(v)
        return p.value()
        
    async def cmd_adc(self, n):
        """
        Read an analog pin.
        """
        p=machine.ADC(n)
        return p.read_u16()  # XXX this is probably doing a sync wait

    async def run(self):
        await self.request.wait_ready()
        await self.request.send_nr("link",True)

class StdBase(BaseCmd):
    #Standard toplevel base implementation

    def __init__(self, parent, fallback=None, state=None, cfg={}, **k):
        super().__init__(parent, **k)

        self.is_fallback=fallback
        self.moat_state=state
        self.cfg = cfg

        self.dis_sys = SysCmd(self)

    async def cmd_ping(self, m=None):
        """
        Echo @m.

        This is for humans. Don't use it for automated keepalive.
        """
        print("PING",m, file=usys.stderr)
        return "R:"+str(m)

