# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""Talk to an ethtimer device.

    from ethtimer import Device
    with Device("COM10") as d:
        print(d.info())
        d.set_net(ip="192.168.100.20", mask="255.255.255.0",
                  gw="192.168.100.1", victim="192.168.100.10")
        d.set_target(port=161, timeout_ms=200)
        reply = d.oneshot(request_bytes)          # warms ARP, gives the length
        d.set_request(request_bytes)
        d.set_window(off=92, length=32)
        batch = d.run(n=100_000, gap_us=200)

`batch.window`, `batch.dt_hw`, `batch.dt_sw` are numpy arrays; `batch.n_short`
and `batch.n_timeout` are the two numbers that say whether to believe them.

WHY `assert_config` EXISTS. The instrument this replaces held its capture
window, port and timeout as sticky state with no way to read them back, so a
window left over from a previous run silently produced zero records and looked
exactly like a dead victim. Every setter here therefore verifies against a
GET_CONFIG read-back, and `run()` refuses to start if the device's state is not
the state the caller asked for.
"""
from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field

import numpy as np
import serial

from . import proto as P


def _ip_bytes(s) -> bytes:
    if isinstance(s, (bytes, bytearray)) and len(s) == 4:
        return bytes(s)
    return socket.inet_aton(s)


def _ip_str(b: bytes) -> str:
    return socket.inet_ntoa(bytes(b))


@dataclass
class Info:
    proto_version: int
    fw_version: int
    clock_hz: int
    max_req: int
    max_win: int
    ring_bytes: int
    link: int
    board: str
    #: v2. How much the request bank holds -- bytes and entries. Reported so a
    #: host sizes its uploads from the board in front of it rather than from a
    #: constant it was built with.
    bank_bytes: int = 0
    bank_max: int = 0

    @property
    def link_name(self) -> str:
        return P.LINK_NAME.get(self.link, "?")

    def __str__(self) -> str:
        return ("%s fw=%d proto=%d clk=%d Hz link=%s max_req=%d max_win=%d "
                "ring=%d B bank=%d B/%d"
                % (self.board, self.fw_version, self.proto_version,
                   self.clock_hz, self.link_name, self.max_req,
                   self.max_win, self.ring_bytes, self.bank_bytes,
                   self.bank_max))


@dataclass
class Config:
    ip: str
    mask: str
    gw: str
    victim: str
    port: int
    timeout_ms: int
    proto: int
    win_off: int
    win_len: int
    req_len: int
    req_crc: int
    link: int
    #: v2. `want` is how many reply bytes an exchange waits for; 0 means
    #: win_off + win_len, which is what every datagram target wants.
    want: int = 0
    bank_n: int = 0
    bank_crc: int = 0
    tcp_state: int = 0
    #: Bytes the device's UART receiver lost. Non-zero means a command frame
    #: may never have completed, which from here looks like a dead board.
    uart_lost: int = 0

    @property
    def link_name(self) -> str:
        return P.LINK_NAME.get(self.link, "?")

    @property
    def tcp_name(self) -> str:
        return P.TCP_NAME.get(self.tcp_state, "?")


@dataclass
class Batch:
    window: np.ndarray            # (n, win_len) uint8
    dt_hw: np.ndarray             # (n,) uint32, DWT cycles
    dt_sw: np.ndarray             # (n,) uint32
    seq: np.ndarray               # (n,) uint32, index within the batch
    n_requested: int
    n_timeout: int
    n_short: int
    elapsed_ms: int
    clock_hz: int
    win_off: int
    #: Exchanges rejected because dt_hw was not a plausible duration -- the
    #: receive interrupt latched before the transmit-complete one and the
    #: subtraction wrapped. One of these leaves the median intact and the
    #: standard deviation meaningless, so they are dropped, not recorded.
    n_bad_dt: int = 0
    meta: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.dt_hw)

    @property
    def rate(self) -> float:
        return len(self) / (self.elapsed_ms / 1000.0) if self.elapsed_ms else 0.0

    def us(self) -> np.ndarray:
        """dt_hw in microseconds."""
        return self.dt_hw.astype(np.float64) / self.clock_hz * 1e6

    def summary(self) -> str:
        return ("%d records of %d requested, %d timeouts, %d short, "
                "%d bad dt, %.0f/s, median %.3f us"
                % (len(self), self.n_requested, self.n_timeout,
                   self.n_short, self.n_bad_dt, self.rate,
                                    float(np.median(self.us())) if len(self) else float("nan")))


class DeviceError(Exception):
    pass


class Device:
    def __init__(self, port: str, baud: int = 921600, timeout: float = 2.0):
        self.ser = serial.Serial(port, baud, timeout=timeout)
        self.dec = P.Decoder()
        self._pending = []
        time.sleep(0.2)
        self.ser.reset_input_buffer()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    # ---- framing ---------------------------------------------------------
    def _send(self, type_: int, payload: bytes = b""):
        self.ser.write(P.encode(type_, payload))

    def _next(self, timeout: float = 5.0):
        """One frame, or raise. Device errors are raised, not returned.

        READ WHAT HAS ARRIVED, NOT A FIXED BLOCK. `read(4096)` waits for 4 096
        bytes or the port timeout, whichever comes first -- so a nine-byte ACK
        cost a FULL PORT TIMEOUT before this call returned, every time. At the
        2 s default that was 2 s per command: a measured 2 009 ms per PING
        round trip, and roughly four seconds added to every batch, which had
        been read as the instrument being slow rather than as a host bug.

        `read(1)` returns the moment a byte lands, and `in_waiting` then drains
        the rest in one call, so bulk transfers still read in large chunks.
        """
        t0 = time.time()
        while not self._pending:
            n = self.ser.in_waiting
            chunk = self.ser.read(n) if n else self.ser.read(1)
            if chunk:
                self._pending.extend(self.dec.feed(chunk))
            elif time.time() - t0 > timeout:
                raise DeviceError("timed out waiting for a frame")
        type_, payload = self._pending.pop(0)
        if type_ == P.RSP_ERR:
            cmd, code = payload[0], payload[1]
            msg = payload[2:].decode("ascii", "replace")
            raise DeviceError("cmd 0x%02x: %s (%s)"
                              % (cmd, P.ERR_NAME.get(code, code), msg))
        return type_, payload

    def _cmd_ack(self, type_: int, payload: bytes = b""):
        self._send(type_, payload)
        t, p = self._next()
        if t != P.RSP_ACK or p[0] != type_:
            raise DeviceError("expected ACK for 0x%02x, got 0x%02x" % (type_, t))

    # ---- queries ---------------------------------------------------------
    def info(self) -> Info:
        self._send(P.CMD_GET_INFO)
        t, p = self._next()
        if t != P.RSP_INFO:
            raise DeviceError("expected INFO, got 0x%02x" % t)
        pv, fv = p[0], p[1]
        clk, maxreq, maxwin, ring = struct.unpack("<IHHI", p[2:14])
        if pv < 2:
            # A v1 board has no bank and no TCP, and its INFO puts the board
            # name where v2 puts the bank limits. Parse it as what it is and
            # let the capability checks name it; misparsing it as v2 would
            # rename the board to four bytes of integer.
            return Info(pv, fv, clk, maxreq, maxwin, ring, p[14],
                        p[15:].decode("ascii", "replace"))
        bank_bytes, bank_max = struct.unpack("<IH", p[15:21])
        return Info(pv, fv, clk, maxreq, maxwin, ring, p[14],
                    p[21:].decode("ascii", "replace"),
                    bank_bytes=bank_bytes, bank_max=bank_max)

    def config(self) -> Config:
        self._send(P.CMD_GET_CONFIG)
        t, p = self._next()
        if t != P.RSP_CONFIG:
            raise DeviceError("expected CONFIG, got 0x%02x" % t)
        ip, mask, gw, vic = p[0:4], p[4:8], p[8:12], p[12:16]
        port, tmo = struct.unpack("<HH", p[16:20])
        pr = p[20]
        woff, wlen, rlen, rcrc = struct.unpack("<HHHH", p[21:29])
        c = Config(_ip_str(ip), _ip_str(mask), _ip_str(gw), _ip_str(vic),
                   port, tmo, pr, woff, wlen, rlen, rcrc, p[29])
        if len(p) >= 39:                      # v2 tail
            c.want, c.bank_n, c.bank_crc = struct.unpack("<HHH", p[30:36])
            c.tcp_state = p[36]
            c.uart_lost = struct.unpack("<H", p[37:39])[0]
        return c

    # ---- setters, each verified by read-back ------------------------------
    def set_net(self, ip, mask, gw, victim):
        self._cmd_ack(P.CMD_SET_NET,
                      _ip_bytes(ip) + _ip_bytes(mask) + _ip_bytes(gw)
                      + _ip_bytes(victim))
        c = self.config()
        if (c.ip, c.mask, c.gw, c.victim) != (_ip_str(_ip_bytes(ip)),
                                              _ip_str(_ip_bytes(mask)),
                                              _ip_str(_ip_bytes(gw)),
                                              _ip_str(_ip_bytes(victim))):
            raise DeviceError("SET_NET did not take: device holds %s/%s gw %s -> %s"
                              % (c.ip, c.mask, c.gw, c.victim))
        return c

    def set_target(self, port: int, timeout_ms: int = 200,
                   protocol: int = P.PROTO_UDP):
        self._cmd_ack(P.CMD_SET_TARGET,
                      struct.pack("<BHH", protocol, port, timeout_ms))
        c = self.config()
        if (c.port, c.timeout_ms, c.proto) != (port, timeout_ms, protocol):
            raise DeviceError("SET_TARGET did not take")
        return c

    def set_request(self, data: bytes):
        if len(data) > P.MAX_REQ:
            raise ValueError("request is %d bytes, device holds %d"
                             % (len(data), P.MAX_REQ))
        self._cmd_ack(P.CMD_SET_REQUEST, bytes(data))
        c = self.config()
        if c.req_len != len(data) or c.req_crc != P.crc16(bytes(data)):
            raise DeviceError("SET_REQUEST did not take: device holds %d bytes "
                              "crc %04x" % (c.req_len, c.req_crc))
        return c

    def set_window(self, off: int, length: int, want: int = 0):
        """Keep `length` bytes at `off`, and wait for `want` bytes of reply.

        `want` is for TCP, where a reply is a byte stream that may be split
        across segments and only the host knows how long a complete one is. A
        datagram arrives whole, so UDP targets leave it at 0, which the device
        reads as off + length.
        """
        payload = (struct.pack("<HHH", off, length, want) if want
                   else struct.pack("<HH", off, length))
        self._cmd_ack(P.CMD_SET_WINDOW, payload)
        c = self.config()
        if (c.win_off, c.win_len, c.want) != (off, length, want):
            raise DeviceError("SET_WINDOW did not take: device holds %d+%d "
                              "want %d" % (c.win_off, c.win_len, c.want))
        return c

    # ---- v2: the request bank -------------------------------------------
    def require(self, version: int, what: str):
        """Refuse early, and say which half is stale.

        A v1 board answers an unknown command with ET_ERR_BAD_ARG, which reads
        as "bad argument" and sends the reader looking at their arguments. This
        names the actual problem.
        """
        got = self.info().proto_version
        if got < version:
            raise DeviceError(
                "%s needs protocol v%d; this board speaks v%d. Rebuild and "
                "reflash it: make -C firmware BOARD=<board>"
                % (what, version, got))

    def batch_capacity(self) -> int:
        """How many exchanges ONE run can hold, from the record ring.

        The device silently shortens a longer run to this, and reports the
        shortened count in BATCH_HDR. That is survivable for a repeated request
        and NOT survivable for a bank: the unplayed entries are discarded with
        the bank, and for a protocol whose requests carry a sequence number the
        host has then advanced past records the victim never saw. A TLS session
        answers that with a fatal alert one bank later, a long way from the
        cause -- so `run()` refuses rather than lets it happen.
        """
        info = self.info()
        cfg = self.config()
        return max(1, info.ring_bytes // (cfg.win_len + P.REC_OVERHEAD))

    def bank_capacity(self, entry_len: int = 0) -> int:
        """How many entries one bank upload can hold on THIS board.

        Sized from GET_INFO rather than from a constant, because the F429 is
        built with a smaller bank than the F746 and a host that assumed either
        one would be wrong on the other.
        """
        info = self.info()
        if not info.bank_max:
            raise DeviceError("this board reports no request bank (protocol "
                              "v%d)" % info.proto_version)
        if entry_len <= 0:
            return info.bank_max
        return max(1, min(info.bank_max, info.bank_bytes // entry_len))

    def set_request_bank(self, requests):
        """Upload a run of DISTINCT requests for the next `run()` to play once.

        Packs as many entries into each frame as fit, because an ACK per entry
        would be a serial round trip per entry -- which is what holds a
        host-in-the-loop relay to tens of exchanges a second. The whole upload
        is then verified in one GET_CONFIG read-back against a CRC the host
        computes over what it meant to send, so a dropped frame is caught here
        rather than after a capture that played the wrong sequence numbers.
        """
        self.require(2, "the request bank")
        reqs = [bytes(r) for r in requests]
        if not reqs:
            raise ValueError("an empty bank would leave the device repeating "
                             "the single held request")
        info = self.info()
        total = sum(len(r) for r in reqs)
        if len(reqs) > info.bank_max or total > info.bank_bytes:
            raise ValueError(
                "bank of %d entries / %d bytes exceeds this board's %d / %d"
                % (len(reqs), total, info.bank_max, info.bank_bytes))
        for r in reqs:
            if len(r) > P.MAX_REQ:
                raise ValueError("a bank entry is %d bytes, device holds %d"
                                 % (len(r), P.MAX_REQ))

        flags = P.BANK_RESET
        i = 0
        while i < len(reqs):
            body = bytearray([flags])
            while i < len(reqs):
                e = struct.pack("<H", len(reqs[i])) + reqs[i]
                if len(body) + len(e) > P.MAX_FRAME_IN:
                    break
                body.extend(e)
                i += 1
            if len(body) == 1:
                raise ValueError("a single request does not fit in one frame")
            self._cmd_ack(P.CMD_SET_BANK, bytes(body))
            flags = 0

        c = self.config()
        want_crc = P.crc16(b"".join(reqs))
        if c.bank_n != len(reqs) or c.bank_crc != want_crc:
            raise DeviceError(
                "SET_BANK did not take: device holds %d entries crc %04x, "
                "host sent %d entries crc %04x.%s"
                % (c.bank_n, c.bank_crc, len(reqs), want_crc,
                   (" The device has lost %d UART bytes, so the upload was "
                    "truncated on the wire rather than rejected."
                    % c.uart_lost) if c.uart_lost else ""))
        return c

    # ---- v2: TCP ---------------------------------------------------------
    def tcp_connect(self):
        """Open the connection every later exchange runs on."""
        self.require(2, "the TCP transport")
        self._cmd_ack(P.CMD_TCP_CONNECT)
        c = self.config()
        if c.tcp_state != P.TCP_OPEN:
            raise DeviceError("TCP_CONNECT acked but the device reports %s"
                              % c.tcp_name)
        return c

    def tcp_close(self):
        try:
            self._cmd_ack(P.CMD_TCP_CLOSE)
        except DeviceError:
            pass

    def relay(self, data: bytes, want: int = 0, timeout: float = 10.0):
        """One TCP exchange with the host in the loop; the WHOLE reply comes back.

        For a handshake, where a record's content depends on the one before it
        and nothing can be pre-generated. The bulk phase uses `set_request_bank`
        instead, which is the same measurement without a serial round trip per
        exchange.

        Returns `(reply_bytes, dt_hw, dt_sw)`; `dt_hw == 0` means no
        transmit-complete interrupt was seen for this exchange and the timing
        is not this exchange's.
        """
        self.require(2, "RELAY")
        self._send(P.CMD_RELAY, struct.pack("<H", want) + bytes(data))
        t, p = self._next(timeout=timeout)
        if t != P.RSP_RELAY:
            raise DeviceError("expected RELAY, got 0x%02x" % t)
        dt_hw, dt_sw, rlen = struct.unpack("<IIH", p[0:10])
        return bytes(p[12:12 + rlen]), dt_hw, dt_sw

    # ---- actions ---------------------------------------------------------
    def oneshot(self, data: bytes = b"") -> bytes:
        """Send once and return the reply, or b'' on timeout.

        Use this before a batch: it warms the ARP entry AND tells you the reply
        length, which is what a window has to fit inside.
        """
        self._send(P.CMD_ONESHOT, bytes(data))
        t, p = self._next()
        if t != P.RSP_ONESHOT:
            raise DeviceError("expected ONESHOT, got 0x%02x" % t)
        return bytes(p)

    def reset(self):
        """Reboot the device. The serial port must be reopened afterwards."""
        self._send(P.CMD_RESET)
        try:
            self._next(timeout=2.0)
        except DeviceError:
            pass

    def capture(self, n: int, gap_us: int = 0, progress=None,
                check_link: bool = True) -> Batch:
        """Capture `n` exchanges, in as many device batches as it takes.

        A single RUN is bounded by the device's record ring -- 4 468 exchanges
        at a 32-byte window on the F746 -- so any real capture is several
        batches. This joins them and SUMS the diagnostics, so `n_short` and
        `n_timeout` still describe the whole capture rather than its last
        batch.

        Batches are independent in the only way that matters for this kind of
        work: the victim keeps advancing its own state between them, so no
        input is repeated across a boundary. What it does NOT do is hide a
        short batch -- if the device returns fewer records than asked for, the
        shortfall is visible in the returned counts rather than silently
        retried.
        """
        info = self.info()
        cfg = self.config()
        rec_len = cfg.win_len + P.REC_OVERHEAD
        per = max(1, info.ring_bytes // rec_len)

        parts, got = [], 0
        while got < n:
            want = min(per, n - got)
            b = self.run(want, gap_us=gap_us, check_link=check_link)
            parts.append(b)
            got += len(b)
            if progress:
                progress(got, n)
            if len(b) == 0 and b.n_timeout >= want:
                raise DeviceError("a whole batch timed out -- victim not "
                                  "answering (%d timeouts)" % b.n_timeout)

        if len(parts) == 1:
            return parts[0]
        first = parts[0]
        return Batch(
            window=np.concatenate([p_.window for p_ in parts]),
            dt_hw=np.concatenate([p_.dt_hw for p_ in parts]),
            dt_sw=np.concatenate([p_.dt_sw for p_ in parts]),
            seq=np.concatenate([p_.seq for p_ in parts]),
            n_requested=sum(p_.n_requested for p_ in parts),
            n_timeout=sum(p_.n_timeout for p_ in parts),
            n_short=sum(p_.n_short for p_ in parts),
            n_bad_dt=sum(p_.n_bad_dt for p_ in parts),
            elapsed_ms=sum(p_.elapsed_ms for p_ in parts),
            clock_hz=first.clock_hz, win_off=first.win_off,
            meta={"batches": len(parts), "per_batch": per})

    def run(self, n: int, gap_us: int = 0, check_link: bool = True,
            progress=None) -> Batch:
        cfg = self.config()
        if check_link and cfg.link != P.LINK_100F:
            raise DeviceError(
                "link is %s, not 100F. A 10 Mbit link serialises a frame ten "
                "times more slowly and erases the structure this measures; it "
                "reads as up exactly like 100F, so it is refused rather than "
                "corrected for. Reset the board to renegotiate."
                % cfg.link_name)
        if not cfg.req_len and not cfg.bank_n:
            raise DeviceError("no request and no bank loaded -- call "
                              "set_request() or set_request_bank() first")
        ring_cap = max(1, info_ring // (cfg.win_len + P.REC_OVERHEAD))             if (info_ring := self.info().ring_bytes) else 1
        if n > ring_cap:
            raise DeviceError(
                "asked for %d exchanges but this board's record ring holds %d "
                "at a %d-byte window. The device would shorten the run "
                "silently; for a banked capture that discards requests the "
                "host has already counted as sent, which desynchronises any "
                "protocol with a sequence number."
                % (n, ring_cap, cfg.win_len))
        if cfg.bank_n and n > cfg.bank_n:
            # The device would silently shorten the run to the bank it holds.
            # Say so here: a batch that is short because the bank was short
            # reads exactly like a batch that is short because the victim
            # stopped answering.
            raise DeviceError(
                "asked for %d exchanges but the bank holds %d. A bank is "
                "played once and never cycled, so the run cannot be longer "
                "than it." % (n, cfg.bank_n))

        self._send(P.CMD_RUN, struct.pack("<II", n, gap_us))

        t, p = self._next(timeout=10.0)
        if t != P.RSP_BATCH_HDR:
            raise DeviceError("expected BATCH_HDR, got 0x%02x" % t)
        n_req, rec_len, win_off, win_len, clock_hz = struct.unpack("<IHHHI", p)

        raw = bytearray()
        # A batch is bounded by the device's ring, so this cannot run away; the
        # timeout is generous because the device is busy measuring, not talking.
        while True:
            t, p = self._next(timeout=max(30.0, n * 0.01))
            if t == P.RSP_BATCH_DATA:
                raw.extend(p)
                if progress:
                    progress(len(raw) // rec_len if rec_len else 0, n_req)
            elif t == P.RSP_BATCH_END:
                got, n_to, n_short, elapsed = struct.unpack("<IIII", p[:16])
                # v2 tail: exchanges whose dt_hw was not a duration. See the
                # firmware's `max_dt`. Counted separately from `n_short`
                # because they are a different fault with a different cause,
                # and folding them together would hide a rate that is worth
                # watching.
                n_bad_dt = struct.unpack("<I", p[16:20])[0] if len(p) >= 20 else 0
                break
            else:
                raise DeviceError("unexpected frame 0x%02x during a batch" % t)

        if len(raw) != got * rec_len:
            raise DeviceError("batch truncated: %d bytes for %d records of %d"
                              % (len(raw), got, rec_len))

        a = np.frombuffer(bytes(raw), np.uint8).reshape(got, rec_len) if got else \
            np.zeros((0, rec_len), np.uint8)
        window = a[:, :win_len].copy()
        tail = a[:, win_len:win_len + 12].copy()
        dt = tail.view(np.uint32) if got else np.zeros((0, 3), np.uint32)
        return Batch(window=window,
                     dt_hw=dt[:, 0].copy(), dt_sw=dt[:, 1].copy(),
                     seq=dt[:, 2].copy(),
                     n_requested=n_req, n_timeout=n_to, n_short=n_short,
                     n_bad_dt=n_bad_dt,
                     elapsed_ms=elapsed, clock_hz=clock_hz, win_off=win_off)
