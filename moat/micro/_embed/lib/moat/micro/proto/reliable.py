import sys

from moat.util import NotGiven, Queue, ValueEvent
from moat.util.compat import (
    Event,
    TaskGroup,
    TimeoutError,
    idle,
    print_exc,
    sleep,
    ticks_add,
    ticks_diff,
    ticks_ms,
    wait_for_ms,
)

from .stack import ChannelClosed, _Stacked


class Reliable(_Stacked):
    """
    Message retry.

    This module handles retransmitting missing messages.

    Messages are wrapped in a dict with retransmit data.

    If @persist is set, the protocol stack above this module is persistent,
    i.e. detached from the taskgroup the connection is running in.
    In this mode you *must* call `run_p()`.

    """

    # Operation:
    # Both sender and receiver carry two message counter: head and tail.
    #
    # Sending, "head" is the next message to be sent, "tail" points to the
    # oldest message that's not yet acknowledged. "tail" is populated (if
    # different from "head"), "head" is not, those in between may not be if
    # they have been selectively acknowledged.
    #
    # Receiving, "head" is the next message we expect, "tail" is the first
    # message we have not received yet. The data at both "head" and "tail"
    # must be empty (obviously). Between those there may be messages that
    # have been received out of order.
    #
    # Both sender and receiver limit the difference from tail to head to
    # window/2. The sender blocks until there is queue space while the
    # receiver discards messages outside this window. In order not to block
    # its clients, the sender also maintains a queue of waiting packets.
    #
    # All messages contain send_head `s`, recv_tail `r`, recv_done `x`,
    # and data `d`. "recv_done" is the list of messages that have been
    # received out-of-order so they're not retransmitted.
    # The receiver advances its send_tail until it matches `r` and queues
    # all messages for retransmission that are still outstanding but not
    # mentioned in `x`.
    #
    # Connection reset is signalled by s=r=-1 and answered with s=r=0
    # before regular message exchange can take place. This exchange must
    # happen in both directions. Other messages received during a reset
    # are discarded.

    rq = None
    __tg = None

    def __init__(self, parent, window=8, timeout=1000, persist=False, **k):
        super().__init__(parent, **k)

        if window < 4:
            raise RuntimeError(f"window must be >=4, not {window}")
        self.window = window
        self.in_reset = None
        self.timeout = timeout
        self._trigger = Event()
        self._is_up = Event()
        self.closed = True
        self.persist = persist

    def reset(self, level=1):
        self.s_send_head = 0  # next to be transmitted
        self.s_send_tail = 0  # no open messages before this point
        self.s_recv_head = 0  # next expected message. Messages before this are out of sequence
        self.s_recv_tail = 0  # messages before this have been processed
        self.s_q = []
        self.m_send = {}
        self.m_recv = {}
        self.t_recv = None
        self.progressed = False
        self.in_reset = ticks_ms()
        self.reset_level = level
        self.pend_ack = True
        self.closed = False
        if self.rq is None:
            self.rq = Queue(self.window)

    async def send_msg(self, k=None):
        self.progressed = True
        if k is None:
            if not self.pend_ack:
                return
            msg = {'s': self.s_send_head}
        else:
            mte = self.m_send[k]
            mte[1] = ticks_add(ticks_ms(), self.timeout)
            msg = {'s': k, 'd': mte[0]}
        msg['r'] = r = self.s_recv_tail
        x = []
        while r != self.s_recv_head:
            if r in self.m_recv:
                x.append(r)
            r = (r + 1) % self.window
        if x:
            msg['x'] = x
        self.pend_ack = False

        try:
            await self.parent.send(msg)
        except RuntimeError:
            # print("NOSEND RESET", self.reset_level, file=sys.stderr)
            pass

        if k is not None and self.m_send.get(k, None) is mte:
            mte[1] = ticks_add(ticks_ms(), self.timeout)

    async def _run_bg(self):
        while not self.closed:
            t = ticks_ms()
            # calculate time to next action
            ntx = None if self.t_recv is None else ticks_diff(self.t_recv, t)
            nk = None
            for k, mte in self.m_send.items():
                m, tx, e = mte
                txd = ticks_diff(tx, t)
                if (ntx is None or ntx > txd) and not e.is_set():
                    ntx = txd
                    nk = k

            if self.s_q and (self.s_send_head - self.s_send_tail) % self.window < self.window // 2:
                # print(f"R {self.parent.txt}: tx", file=sys.stderr)
                pass
            elif ntx is None:
                # print(f"R {self.parent.txt}: inf", file=sys.stderr)
                await self._trigger.wait()
                self._trigger = Event()
            elif ntx > 0:
                # print(f"R {self.parent.txt}: {ntx}", file=sys.stderr)
                try:
                    await wait_for_ms(ntx, self._trigger.wait)
                except TimeoutError:
                    pass
                else:
                    self._trigger = Event()
            else:
                # print(f"R {self.parent.txt}: now {ticks_ms()}", file=sys.stderr)
                pass

            # process pending-send queue
            if self.s_q and (self.s_send_head - self.s_send_tail) % self.window < self.window // 2:
                seq = self.s_send_head
                msg, evt = self.s_q.pop(0)
                nseq = (seq + 1) % self.window
                # print("SH1",self.parent.txt,self.s_send_tail,
                #             self.s_send_head,nseq, file=sys.stderr)
                self.s_send_head = nseq
                self.m_send[seq] = [msg, None, evt]
                await self.send_msg(seq)

            if ntx is not None and ntx <= 0:  # work
                if nk is not None:  # retransmit message K
                    await self.send_msg(nk)
                if self.pend_ack:
                    await self.send_msg()

                if nk is None:
                    self.t_recv = ticks_add(ticks_ms(), self.timeout)

    async def run_p(self, immediate=False):
        """
        Run Persistent.

        If @immediate is set, the upper stack is started even if the lower
        half is not yet ready.
        """
        if not self.persist:
            return
        if not immediate:
            await self.wait()
        await self.child.run()

    async def wait(self):
        """
        Wait until the lower side is (again) ready to be used.

        There is no guarantee that this is still so when you call
        send/recv next.
        """
        while self.closed:
            await self._is_up.wait()

    async def run(self):
        self.reset()
        try:
            async with TaskGroup() as tg:
                self.__tg = tg
                reader = await tg.spawn(self._read, _name="rel_read")
                runner = await tg.spawn(self._run_bg, _name="rel_run")
                while self.in_reset:
                    t = ticks_ms()
                    td = ticks_diff(self.in_reset, t)
                    if td > 0:
                        try:
                            await wait_for_ms(td, self._trigger.wait)
                        except TimeoutError:
                            pass
                        # DO NOT replace self._trigger here. _run_bg() already does that.
                    else:
                        await self.send_reset()

                if self.closed:
                    raise EOFError(self)

                # At this point the module above us can talk, so run it
                # print(f"X {self.parent.txt}: running", file=sys.stderr)
                if self.persist:
                    await idle()
                else:
                    await self.child.run()
                # print(f"X {self.parent.txt}: ending", file=sys.stderr)
                tg.cancel()

        except BaseException as exc:
            err = str(exc)
            raise
        else:
            err = None
        finally:
            self.__tg = None
            cl, self.closed = self.closed, True
            for _m, _t, e in self.m_send.values():
                e.set_error(ChannelClosed())
            while self.s_q:
                _m, e = self.s_q.pop()
                e.set_error(ChannelClosed())
            if self._is_up.is_set():
                self._is_up = Event()
            if not cl:
                msg = {'a': 'r', 'n': 0}
                if err is not None:
                    msg['err'] = err
                try:
                    await self.parent.send(msg)
                except EOFError:
                    pass

    async def send_reset(self, level=0, err=None):
        if self.closed:
            level = 0
        elif level:
            self.reset_level = level
        else:
            level = self.reset_level
        msg = {'a': 'r', 'n': level}
        if level:
            msg['c'] = self._get_config()
        if err is not None:
            msg['e'] = err
        if self.reset_level < 3:
            self.in_reset = ticks_add(ticks_ms(), self.timeout)
        self._trigger.set()
        await self.parent.send(msg)

    async def send(self, msg):
        if self.closed:
            raise ChannelClosed()
        evt = ValueEvent()
        # print(f"T {self.parent.txt}: SQ {msg} {id(self._trigger)}", file=sys.stderr)
        self.s_q.append((msg, evt))
        self._trigger.set()
        return await evt.wait()

    def _get_config(self):
        return {'t': self.timeout, 'm': self.window}

    def _update_config(self, c):
        self.timeout = max(self.timeout, c.get('t', 0))
        self.window = max(4, min(self.window, c.get('m', self.window)))

    def _reset_done(self):
        if self.in_reset:
            self.in_reset = False
            self._trigger.set()
            self._is_up.set()

    async def recv(self):
        return await self.rq.get()

    async def _read(self):
        while True:
            try:
                msg = await self.parent.recv()
            except EOFError:
                if self.__tg is not None:
                    self.__tg.cancel()
                return
            await self.dispatch(msg)

    async def dispatch(self, msg):
        a = msg.get('a', None)

        if a is None:
            pass
        elif a == 'r':
            c = msg.get('c', {})
            n = msg.get('n', 0)
            e = msg.get('e', None)
            if n == 0:  # closed
                self.closed = True
                if self._is_up.is_set():
                    self._is_up = Event()
                self._trigger.set()
            elif self.closed:
                await self.send_reset()
                return
            elif n == 1:  # incoming reset
                if self.in_reset:
                    if self.reset_level == 1:
                        self.reset_level = 2
                else:
                    self.reset(2)
                    await self.error(RuntimeError(e or "ext reset"))
                self._update_config(c)
                await self.send_reset()
                return
            elif n == 2:  # incoming ack
                self._update_config(c)
                await self.send_reset(3)
                self._reset_done()
                return
            elif n == 3:  # incoming ack2
                if not self.in_reset or self.reset_level > 1:
                    self._update_config(c)
                    self._reset_done()
                else:
                    await self.error(RuntimeError("ext reset ack2"))
                    self.reset(1)
                    await self.send_reset()
                return
            else:
                # ignored
                return
        else:
            return  # unknown action

        if self.closed:
            # if we're down, reply with a reset, but not every time
            if self.reset_level > 2:
                try:
                    await self.send_reset()
                except EOFError:
                    pass
                self.reset_level = 0
            else:
                self.reset_level += 1
            return

        if self.in_reset:
            if self.reset_level < 2:
                await self.send_reset()
                return
            self._reset_done()

        r = msg.get('s', None)  # swapped (our PoV of incoming msg)
        s = msg.get('r', None)
        x = msg.get('x', ())

        if r is None or s is None:
            return
        if not (0 <= r < self.window) or not (0 <= s < self.window):
            self.reset(1)
            await self.send_reset(err="R/S out of bounds")
            return

        d = msg.get('d', NotGiven)
        if d is not NotGiven:
            # data. R is the message's sequence number.
            self.pend_ack = True
            if self.between(self.s_recv_tail, self.s_recv_head, r):
                if (r - self.s_recv_tail) % self.window < self.window // 2:
                    # print("RH1",self.parent.txt,self.s_recv_tail,
                    #             self.s_recv_head,r,r+1, file=sys.stderr)
                    self.m_recv[r] = d
                    self.s_recv_head = (r + 1) % self.window
                else:
                    pass
                    # print("RH1-",self.parent.txt,self.s_recv_tail,
                    #             self.s_recv_head,r,r+1, file=sys.stderr)
            elif self.between(self.s_recv_tail, r, self.s_recv_head):
                self.m_recv[r] = d

        elif self.between(self.s_recv_tail, self.s_recv_head, r):
            # no data. R is the next-expected sequence number.
            if (r - self.s_recv_tail) % self.window <= self.window // 2:
                self.s_recv_head = r
                # print("RH2",self.parent.txt,self.s_recv_tail,
                #             self.s_recv_head,r,r+1, file=sys.stderr)
            else:
                pass
                # print("RH2-",self.parent.txt,self.s_recv_tail,
                #             self.s_recv_head,r,r+1, file=sys.stderr)

        # process ACKs
        if s >= 0:
            rr = self.s_send_tail
            while rr != s:
                if rr == self.s_send_head:
                    # XXX
                    break
                try:
                    _m, _t, e = self.m_send.pop(rr)
                except KeyError:
                    pass
                else:
                    self.pend_ack = True
                    e.set(None)
                rr = (rr + 1) % self.window
                self._trigger.set()

            # print("ST1",self.parent.txt,self.s_send_tail,self.s_send_head,rr, file=sys.stderr)
            self.s_send_tail = rr

        for rr in x:
            try:
                _m, _t, e = self.m_send[rr]
            except KeyError:
                pass
            else:
                e.set(None)

        # Forward incoming messages if s_recv[recv_tail] has arrived
        rr = self.s_recv_tail
        while rr != self.s_recv_head:
            try:
                d = self.m_recv.pop(rr)
            except KeyError:
                break
            else:
                rr = (rr + 1) % self.window
                self.s_recv_tail = rr
                # print("RT1",self.parent.txt,self.s_recv_tail,
                #             self.s_recv_head,r,r+1, file=sys.stderr)
                self.pend_ack = True
                await self.rq.put(d)

        if self.s_recv_tail == self.s_recv_head:
            self.t_recv = None
        else:
            self.t_recv = ticks_add(ticks_ms(), self.timeout)
            self._trigger.set()

        if self.pend_ack:
            # TODO delay ACK somewhat
            try:
                await self.send_msg()
            except EOFError:
                pass

    def between(self, a, b, c):
        d1 = (b - a) % self.window
        d2 = (c - a) % self.window
        return d1 <= d2
