# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""DNS as an `ethtimer` target: the measurement with nothing to set up.

    python -m ethtimer.cli --target dns --n 20000 \
        --ip 192.168.1.50 --mask 255.255.255.0 --gw 192.168.1.1 \
        --victim 192.168.1.1

THE POINT OF THIS DEMO is that the thing being measured is already on your
network. Every other adapter here needs a device built and flashed with known
credentials; this one needs a resolver, and a home router is one. So it is the
demo to run **first**, before trusting a number from any of the others: it
exercises the whole path -- the build, the link, the serial framing, the window,
the batching and the record layout -- against something you did not have to
prepare.

IT IS ALSO THE DEMO WITH NO ACCEPTANCE CHECK, and that is the whole reason the
other three exist. There is no key here, so `campaign` cannot say "these bytes
are what that device's AES produced"; it prints `NOT CHECKED` and
`python -m ethtimer.cli` exits **2**. A completed DNS capture tells you the
instrument works. It does not tell you a window is where you think it is, and
that distinction is the one this repository keeps making.

WHAT IT DOES CHECK, without a key. The window is the reply's **question
section**, which a resolver echoes from the query byte for byte. So every record
should hold the same bytes, and `blocks()` raises if they do not -- which is a
real check on the window offset, the record stride and the framing, available
with no secret at all. The answer section is deliberately *excluded*: its TTL
counts down, so keeping it would make a correct capture look inconsistent.

MODE_BANK, FOR A REASON WORTH SEEING. A resolver may well collapse a repeated
query -- same transaction ID, same question, arriving back to back -- into one
response, or rate-limit it. So each exchange gets a fresh transaction ID from
the request bank, which also means this demo exercises the bank upload path that
the OSCORE and TLS adapters depend on. `--n 20000` against a LAN resolver is a
few dozen bank uploads.

WHAT THE TIMING MEANS HERE. A cached answer from a router is a few hundred
microseconds of that router's own turnaround and says nothing about any
cryptography. Treat the distribution as a property of the box you pointed it at.
"""
from __future__ import annotations

import os
import struct
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from ethtimer.target import MODE_BANK, Blocks, Target, register  # noqa: E402

#: The name to ask about. Anything a resolver answers from cache will do; a
#: name that does not resolve gets an NXDOMAIN, which is still a timed exchange
#: and still has a question section, so the demo works either way.
QNAME = b"example.com"
#: A record.
QTYPE = 1
#: IN.
QCLASS = 1

#: DNS header is 12 bytes: id, flags, 4 counts. The question follows it.
HEADER_LEN = 12


def encode_name(name: bytes) -> bytes:
    """`example.com` -> `\\x07example\\x03com\\x00`, RFC 1035 s4.1.2."""
    out = bytearray()
    for label in name.split(b"."):
        if not 1 <= len(label) <= 63:
            raise ValueError("bad DNS label %r in %r" % (label, name))
        out.append(len(label))
        out += label
    out.append(0)
    return bytes(out)


def query(txid: int, name: bytes = QNAME,
          qtype: int = QTYPE, qclass: int = QCLASS) -> bytes:
    """A standard recursive query with one question and no EDNS.

    No EDNS deliberately: an OPT record would put a varying pseudo-section in
    the reply, and the window is meant to be the one part of it that holds
    still.
    """
    #                  id    flags   qd an ns ar
    head = struct.pack(">HHHHHH", txid & 0xFFFF, 0x0100, 1, 0, 0, 0)
    return head + encode_name(name) + struct.pack(">HH", qtype, qclass)


@register
class DnsTarget(Target):
    name = "dns"
    port = 53
    mode = MODE_BANK
    #: A LAN resolver answers from cache in well under a millisecond; a
    #: recursive miss can take far longer, and a timeout is reported rather
    #: than waited on forever.
    timeout_ms = 200

    def __init__(self, qname: bytes = QNAME):
        self.qname = qname
        self._qlen = len(encode_name(qname)) + 4      # name + qtype + qclass
        self._txid = 0
        self._first_window = None

    # ---- setup ----------------------------------------------------------
    def prepare(self, dev):
        """Confirm the resolver answers, and that it echoes the question.

        Done here rather than left to the capture because a resolver that
        answers a *different* question -- or truncates -- produces a window of
        plausible bytes that mean nothing, and that is cheaper to find now than
        in `blocks()` twenty thousand exchanges later.
        """
        probe = query(0xABCD, self.qname)
        reply = b""
        for _ in range(5):
            reply = dev.oneshot(probe)
            if reply:
                break
        if not reply:
            raise RuntimeError(
                "no answer from the resolver on UDP 53. Check --victim is a "
                "resolver reachable from --ip/--gw, and that the board's link "
                "is up: `python -m ethtimer.cli --ports` then `d.info()` "
                "reports the link state.")
        if len(reply) < HEADER_LEN + self._qlen:
            raise RuntimeError(
                "the reply is %d bytes, shorter than a header plus the question "
                "(%d). That is not a DNS response to this query."
                % (len(reply), HEADER_LEN + self._qlen))
        echoed = reply[HEADER_LEN:HEADER_LEN + self._qlen]
        if echoed != probe[HEADER_LEN:]:
            raise RuntimeError(
                "the resolver did not echo the question section:\n  sent %r\n  "
                "got  %r\nThe window here is that echo, so there is nothing "
                "stable to keep." % (probe[HEADER_LEN:], echoed))
        rcode = struct.unpack(">H", reply[2:4])[0] & 0x0F
        ancount = struct.unpack(">H", reply[6:8])[0]
        print("dns: %s -> rcode %d, %d answer(s), %d B reply"
              % (self.qname.decode("ascii", "replace"), rcode, ancount,
                 len(reply)))

    def requests(self):
        """A fresh transaction ID per exchange, forever.

        Wraps at 16 bits, which is what the field is. A capture longer than
        65 536 exchanges therefore reuses IDs -- spaced that far apart, no
        resolver treats them as the duplicate of a query it is still holding.
        """
        while True:
            self._txid = (self._txid + 1) & 0xFFFF
            yield query(self._txid, self.qname)

    def window(self, reply: bytes):
        """The question section: what the resolver echoed, and nothing after it.

        NOT the answer. A cached A record's TTL counts down between exchanges,
        so an answer section that changes is a correct capture of a changing
        reply -- indistinguishable, from the bytes alone, from a window that has
        drifted. Keeping only the echo makes "the bytes are not constant" mean
        exactly one thing.
        """
        return HEADER_LEN, self._qlen

    # ---- results --------------------------------------------------------
    def blocks(self, window: np.ndarray, index: np.ndarray) -> Blocks:
        """No AES pairs -- and a check that the window held still.

        Returns `(n, 0, 16)` arrays, which is how a target says "nothing to
        verify cryptographically". `campaign` reports that honestly instead of
        passing quietly, because `truth_key()` returns None.
        """
        expect = np.frombuffer(encode_name(self.qname)
                               + struct.pack(">HH", QTYPE, QCLASS), np.uint8)
        if window.shape[1] != len(expect):
            raise RuntimeError(
                "kept %d bytes per record, expected %d: the window the device "
                "held is not the one this adapter asked for"
                % (window.shape[1], len(expect)))
        same = (window == expect).all(axis=1)
        bad = int((~same).sum())
        if bad:
            first = int(np.where(~same)[0][0])
            raise RuntimeError(
                "%d of %d records do not hold the echoed question section, "
                "first at record %d (capture exchange %d).\n  expected %s\n  "
                "got      %s\nEvery reply to this query echoes the same bytes "
                "there, so a difference is the capture path and not the "
                "resolver: a window offset, a record stride, or a reply that "
                "was not a response to this query."
                % (bad, len(window), first,
                   int(index[first]) if len(index) > first else -1,
                   bytes(expect).hex(), bytes(window[first]).hex()))
        print("dns: question section identical on all %d records" % len(window))
        n = len(window)
        empty = np.zeros((n, 0, 16), np.uint8)
        return Blocks(aes_in=empty, aes_out=empty,
                      extra=dict(question=np.frombuffer(bytes(expect), np.uint8)))

    def truth_key(self):
        """None: there is no key in DNS, and `campaign` says so out loud.

        This is not an oversight to be papered over with a record count. It is
        the difference between "the instrument moved bytes" and "the bytes are
        the ones that device produced", and only the second is a measurement you
        can publish.
        """
        return None
