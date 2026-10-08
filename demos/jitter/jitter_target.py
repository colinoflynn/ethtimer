# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""The reference responder as an `ethtimer` target: measure the PATH, not an endpoint.

    python -m ethtimer.cli --target jitter --n 50000 \\
        --ip 192.168.7.20 --gw 192.168.7.1 --victim 192.168.7.10 \\
        --out jitter.npz
    python demos/jitter/analyse.py jitter.npz

WHAT THIS DEMO IS FOR. Every other adapter here measures a device doing real
work, and the number it returns is that device's turnaround. This one measures
a responder that does as little as possible and **reports its own interval in
every reply**, so the host can take it off:

    dt_hw        instrument transmit-complete -> instrument receive interrupt
    victim_us    responder receive interrupt  -> responder send
    path_us      the difference: everything outside both endpoints' software

Put a switch, a router or a long cable between the two boards and `path_us` is
where its contribution shows up. Run a direct cable first and the difference of
the two runs is the device under test.

WHAT `path_us` IS NOT. It is not any device's latency. It is that device's
latency **plus** two frame serialisations, two MAC transmit paths and the
instrument's own interrupt latency -- all constant for a fixed frame size, none
of them zero. On a direct cable at 100 Mbit with a 32-byte payload the constant
is tens of microseconds and it is entirely the two endpoints. So:

* the VARIANCE of `path_us` is the path's variance, and that is the measurement
  this demo is for;
* the MEAN of `path_us` is a number with a constant in it, and the only honest
  way to use it is as a difference between two runs that share the frame size.

`analyse.py` prints it both ways round and says which is which.

THE SECOND USE: VALIDATING A BUILD. The responder is the one target here whose
reply is almost entirely predictable, so a capture against it on a direct cable
is a measurement of the instrument and nothing else. A new board, a new
toolchain or a bumped SDK pin that changed something shows up as a `path_us`
distribution that does not match the last one. Nothing else in `demos/` isolates
the instrument that way, because every other target has a real device's
scheduling in it.

NO ACCEPTANCE CHECK, and `truth_key()` says so. There is no key, so `campaign`
reports NOT CHECKED and the CLI exits 2 -- see `demos/dns/README.md`, which
makes the same point. What takes its place is narrower and real: every record
must carry the responder's magic and version (`etv.check`), and the responder's
own counter must advance by exactly one per record (`etv.continuity`), which is
how a reordered or lost reply is caught. The instrument's timeout count cannot
see reordering at all.
"""
from __future__ import annotations

import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import etv                                                     # noqa: E402
from ethtimer.target import MODE_FIXED, Blocks, Target, register  # noqa: E402

#: Total UDP payload length, each way. The responder mirrors the request's
#: length, so this one number sets the frame size in both directions.
#:
#: 64 by default rather than the 32-byte reply header: it leaves the header
#: room to grow without changing what a saved capture means, and it keeps the
#: Ethernet frame comfortably above the 64-byte minimum so no padding is added
#: by the MAC -- padding that would make the wire frame a different size from
#: the one this number names.
PAD = 64


@register
class JitterTarget(Target):
    name = "jitter"
    port = etv.PORT
    #: The responder has no replay protection and no state that a repeat
    #: disturbs, so one held request runs a whole batch with the host out of
    #: the loop. That is the instrument's fastest path, and a jitter
    #: measurement wants the exchanges close together -- a bank upload every
    #: 1 024 exchanges would put a millisecond-scale gap in the series at
    #: regular intervals, which is a pattern that looks like the path.
    mode = MODE_FIXED
    #: Short on purpose. The responder answers in tens of microseconds on a
    #: direct cable; a 200 ms timeout would turn one dropped frame into 200 ms
    #: of nothing, and at 50 000 exchanges a handful of drops is minutes.
    timeout_ms = 20

    def __init__(self, pad: int = PAD, tag: int = 0x5A5A5A5A):
        self.pad = int(pad)
        self.tag = int(tag)
        self._req = etv.request(tag=self.tag, pad_to=self.pad)

    # ---- setup ----------------------------------------------------------
    def prepare(self, dev):
        """Confirm a reply, that it is this responder, and that it is 100F.

        Done here because all three failures look alike from inside a capture:
        a responder that is not running, one flashed with a different protocol
        version, and a link that negotiated 10 Mbit all produce a batch of
        timeouts or a distribution with nothing in it.
        """
        reply = b""
        for _ in range(10):
            reply = dev.oneshot(self._req)
            if reply:
                break
        if not reply:
            raise RuntimeError(
                "no reply from the responder on UDP %d after 10 probes.\n"
                "  * is firmware/victim flashed on the other board "
                "(make BOARD=<b> APP=victim)?\n"
                "  * does --victim match the address its console printed?\n"
                "  * is the link up at both ends? `Device.info()` reports this "
                "end's." % etv.PORT)
        if len(reply) < etv.RSP_HDR:
            raise RuntimeError(
                "the reply is %d bytes, shorter than the %d-byte reply header. "
                "Something answered on port %d, but it was not this responder."
                % (len(reply), etv.RSP_HDR, etv.PORT))

        one = etv.decode(np.frombuffer(reply[:etv.RSP_HDR], np.uint8)
                         .reshape(1, etv.RSP_HDR))
        etv.check(one)

        self.clk_hz = int(one["clk_hz"][0])
        self.reply_len = len(reply)
        if self.reply_len != self.pad:
            # Not fatal: the responder clamps, and a clamped length is still a
            # usable capture. But the frame size is the thing a jitter sweep
            # varies, so being wrong about it quietly would spoil the sweep.
            print("  *** asked for %d-byte frames, the responder replied with "
                  "%d; the sweep axis is the reply length, not --pad ***"
                  % (self.pad, self.reply_len))

        if not (int(one["flags"][0]) & etv.F_LINK_100F):
            raise RuntimeError(
                "the responder reports its link is not 100 Mbit full duplex. "
                "A 10 Mbit link serialises a frame ten times more slowly, "
                "which is the largest term in the path figure and swamps "
                "whatever you were trying to measure. Settle the link first.")

        vic = float(one["victim_us"][0])
        if not vic > 0.0:
            raise RuntimeError(
                "the responder reports its own interval as %.3f us, which it "
                "cannot be: its cycle counter is not running.\n"
                "Every record would carry zero, so the path figure would be "
                "the whole round trip with nothing taken off -- about nine "
                "times the truth on a direct cable, and marked as wrong "
                "nowhere.\n"
                "Its console says so at start-up too. Power-cycle the "
                "responder; a debugger reset is not always enough. See "
                "dwt_enable() in firmware/boards/*/board.c." % vic)

        print("jitter: responder clock %d Hz, %d-byte frames each way, "
              "its own interval %.3f us on the probe"
              % (self.clk_hz, self.reply_len, vic))

    def requests(self):
        """One request, forever. MODE_FIXED uses only the first."""
        while True:
            yield self._req

    def sample_reply(self):
        return None             # let `campaign` take the ARP-warming probe

    def window(self, reply: bytes):
        """The whole reply header, and nothing after it.

        Not the padding: it is zeroes whose only job is to make the frame
        longer on the wire, and keeping it would cost transfer time per record
        for no information. The header is where every field lives.
        """
        return 0, etv.RSP_HDR

    # ---- results --------------------------------------------------------
    def blocks(self, window: np.ndarray, index: np.ndarray) -> Blocks:
        """No AES pairs. The responder's own fields, checked, as extras."""
        dec = etv.decode(window)
        etv.check(dec)

        cont = etv.continuity(dec)
        if cont["backwards"]:
            print("  *** %d records arrived out of order (the responder's own "
                  "counter went backwards). The instrument's timeout count "
                  "cannot see this. ***" % cont["backwards"])
        if cont["gaps"]:
            print("  %d gaps in the responder's counter, %d replies "
                  "unaccounted for: sent by it and not seen here, or never "
                  "sent." % (cont["gaps"], cont["missing"]))

        wrapped = int((dec["flags"] & etv.F_CYC_WRAP).astype(bool).sum())
        if wrapped:
            print("  %d records span a 32-bit cycle-counter wrap on the "
                  "responder; the subtraction is still correct." % wrapped)

        n = len(window)
        empty = np.zeros((n, 0, 16), np.uint8)
        return Blocks(aes_in=empty, aes_out=empty,
                      extra=dict(victim_cyc=dec["victim_cyc"].astype(np.uint32),
                                 victim_us=dec["victim_us"],
                                 victim_seq=dec["seq"],
                                 victim_hz=dec["clk_hz"],
                                 victim_rx_cyc=dec["rx_cyc"],
                                 victim_tx_cyc=dec["tx_cyc"],
                                 victim_n_seen=dec["n_seen"],
                                 victim_flags=dec["flags"]))

    def truth_key(self):
        """None: there is no key here, and `campaign` says so rather than
        letting a record count read as a pass. `etv.check` and
        `etv.continuity` are what this demo has instead, and they are checks
        on the capture path rather than on the data."""
        return None

    def extra_arrays(self):
        """Constants of the run, for the partial-capture sidecar and the
        analysis: the frame size is the sweep axis and a saved capture that
        does not record it cannot be compared with another."""
        return dict(frame_bytes=np.int64(getattr(self, "reply_len", self.pad)),
                    pad_requested=np.int64(self.pad),
                    responder_hz=np.int64(getattr(self, "clk_hz", 0)))
