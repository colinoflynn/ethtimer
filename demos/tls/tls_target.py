# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""TLS 1.2 HTTPS as an `ethtimer` target: the instrument over TCP.

    python -m ethtimer.cli --target tls --n 5000

WHY TLS RUNS ON THE HOST AND THE BOARD IS A RELAY. A TLS client on the board
would hand the application PLAINTEXT, and what this measures is the turnaround
around the ciphertext that is actually on the wire -- so the side that drives
TLS has to be the side that holds the record bytes. The board therefore carries
a TCP connection and a cycle counter and nothing else, which is exactly what
`ethtimer`'s TCP transport is.

TWO PHASES, TWO MECHANISMS.

* The **handshake** cannot be pre-generated: every record depends on the one
  before it. It runs through `Device.relay()`, one exchange per host round trip,
  which is what RELAY exists for.
* The **bulk phase** can be. The host holds the TLS write state, so it can
  produce a run of request records -- each with its own sequence number and
  explicit IV -- without waiting for any response. They go into the request bank
  and the board walks it. A serial round trip per exchange holds a relay to tens
  of exchanges a second; banking them reaches the device's own limit, which on
  the reference device is 156/s.

FIFTEEN PAIRS PER EXCHANGE. A TLS 1.2 CBC record is
`header || explicit IV || C_1 .. C_n` with `C_i = E(K, P_i ^ C_(i-1))`, and the
response is a fixed 255-byte HTTP page, so for every block of known plaintext
both ends are on the wire:

    aes_in[i]  = P_i ^ C_(i-1)      (C_0 is the IV, sent in clear)
    aes_out[i] = C_i

Fifteen blocks of the response are fully known, and the device encrypted all of
them inside one measured turnaround, so one exchange yields fifteen pairs --
which is the difference between a thirteen-hour capture and a one-hour one. It
is also why `ET_MAX_WIN` has to hold 16 blocks: the IV plus fifteen ciphertexts.
"""
from __future__ import annotations

import os
import ssl
import sys
import tempfile

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import tlskeys                                                  # noqa: E402
from ethtimer.target import MODE_BANK, Blocks, Target, register   # noqa: E402
from ethtimer import proto as P                                # noqa: E402

GET = b"GET /index.html HTTP/1.1\r\nHost: v\r\nConnection: keep-alive\r\n\r\n"

#: Blocks of the response whose plaintext is known. The response is 255 bytes of
#: fixed HTTP followed by a 32-byte MAC and padding, so blocks 1..15 (bytes
#: 0..239) are all inside the known page and block 16 straddles the MAC.
BLOCKS = 15
#: The IV plus those fifteen ciphertext blocks.
WINDOW = 16 * (BLOCKS + 1)


@register
class TlsTarget(Target):
    name = "tls"
    port = 443
    mode = MODE_BANK
    transport = P.PROTO_TCP
    #: Generous next to an 833 us exchange: a TLS record can be split across
    #: segments, and a delayed ACK is not a dead victim.
    timeout_ms = 400

    def __init__(self, keylog=None):
        self._keylog = keylog or os.path.join(tempfile.gettempdir(),
                                              "ethtimer_tls.keys")
        self.client_random = None
        self.server_random = None
        self.cipher = None
        self.page = None                 # the response plaintext, from the wire
        self._server_key = None
        self._want = 0
        self._sample = None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        # The victim is built with one ciphersuite. Asking for it explicitly
        # makes a mismatch an error here rather than a quiet renegotiation into
        # something this adapter cannot parse.
        ctx.set_ciphers("AES128-SHA256")
        # For the acceptance check only. Taking the measurement never reads
        # this; it is how the capture gets checked against the key the
        # device really used.
        ctx.keylog_filename = self._keylog
        self.inb, self.outb = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.obj = ctx.wrap_bio(self.inb, self.outb)

    # ---- setup ----------------------------------------------------------
    def prepare(self, dev):
        self._handshake(dev)
        self._first_response(dev)
        self._derive_key()

    def _handshake(self, dev):
        """Drive the TLS handshake one record at a time through RELAY."""
        for _ in range(40):
            try:
                self.obj.do_handshake()
                break
            except ssl.SSLWantReadError:
                pass
            out = self.outb.read()
            if out and self.client_random is None:
                self.client_random = tlskeys.hello_random(out)
            data, _, _ = dev.relay(out, want=1)
            if data:
                if self.server_random is None:
                    self.server_random = tlskeys.hello_random(data)
                self.inb.write(data)
        else:
            raise RuntimeError(
                "the TLS handshake did not complete in 40 relayed records. "
                "Check the victim is the AES128-SHA256 build and that the TCP "
                "connection stayed open (a relay that returns nothing every "
                "time means the peer closed).")
        out = self.outb.read()
        if out:
            dev.relay(out, want=1)
        self.cipher = self.obj.cipher()
        print("tls: %s" % (self.cipher,))

    def _first_response(self, dev):
        """One request through RELAY, to learn the page and the record length.

        Both are read off the wire rather than assumed: the record length is
        what `want` has to be, and the page is the known plaintext that turns
        ciphertext into an AES input. A constant for either would be a constant
        that is right until the victim's page changes by one byte.
        """
        self.obj.write(GET)
        rec = self.outb.read()
        data, _, _ = dev.relay(rec, want=0)
        if not data:
            raise RuntimeError("the victim did not answer the first HTTPS "
                               "request over the relay")
        self._want = len(data)
        self.inb.write(data)
        page = self.obj.read(4096)
        if len(page) < 16 * BLOCKS:
            raise RuntimeError(
                "the response is %d bytes of plaintext, less than the %d this "
                "target needs for %d known blocks"
                % (len(page), 16 * BLOCKS, BLOCKS))
        self.page = page
        self._sample = data

        # The known plaintext must actually be constant, or every later
        # exchange is paired with the wrong AES input. Check rather than trust:
        # a Date header or a counter in the page would break this silently.
        self.obj.write(GET)
        data2, _, _ = dev.relay(self.outb.read(), want=self._want)
        self.inb.write(data2)
        page2 = self.obj.read(4096)
        if page2 != page:
            raise RuntimeError(
                "the device's response changed between two requests, so it "
                "is not the constant plaintext this adapter assumes:"
                "\n  %r\n  %r"
                % (page[:80], page2[:80]))
        print("tls: response record %d B, page %d B, constant across two "
              "requests" % (self._want, len(page)))

    def _derive_key(self):
        """The server write key, from the NSS key log. Validation only."""
        ms = tlskeys.read_keylog(self._keylog, self.client_random)
        if ms is None:
            print("  *** no master secret in the key log: the capture cannot "
                  "be checked against ground truth ***")
            return
        kb = tlskeys.key_block(ms, self.client_random, self.server_random)
        self._server_key = kb["server_write_key"]
        print("tls: server write key %s (ground truth, acceptance check only)"
              % self._server_key.hex())

    # ---- the capture ----------------------------------------------------
    def sample_reply(self):
        """The response record `prepare()` already fetched over the relay."""
        return self._sample

    def requests(self):
        """Request records, generated without waiting for any response.

        The TLS write state is here, so a run of records can be produced ahead
        of time, each with its own sequence number and explicit IV. The READ
        side is deliberately not advanced: the board returns only a window of
        each response, and the pair this demo forms comes from the window's
        own bytes without decrypting anything.
        """
        while True:
            self.obj.write(GET)
            yield self.outb.read()

    def window(self, reply):
        """Keep the explicit IV and fifteen ciphertext blocks.

        Offset 5 skips the record header. The third element is `want`: how many
        bytes of the response an exchange waits for before it stops collecting,
        which a stream transport needs and a datagram one does not.
        """
        if len(reply) < 5 + WINDOW:
            raise RuntimeError(
                "the response record is %d bytes, too short for the header "
                "plus %d blocks. Expected about %d."
                % (len(reply), BLOCKS + 1, self._want))
        return 5, WINDOW, self._want or len(reply)

    # ---- results --------------------------------------------------------
    def blocks(self, window, index):
        w = np.asarray(window, np.uint8).reshape(len(window), BLOCKS + 1, 16)
        pt = np.frombuffer(self.page[:16 * BLOCKS], np.uint8).reshape(BLOCKS, 16)

        # C_0 is the IV; C_i for i >= 1 are the ciphertext blocks. CBC gives
        # both ends of every block: the input is P_i ^ C_(i-1) and the output
        # is C_i.
        c_prev = w[:, 0:BLOCKS, :]           # C_0 .. C_14
        c_cur = w[:, 1:BLOCKS + 1, :]        # C_1 .. C_15
        aes_in = c_prev ^ pt[None, :, :]
        aes_out = c_cur
        # `window` and `blocks` are the names this demo's `analyse.py` reads;
        # emitting them means a generic capture needs no conversion step.
        return Blocks(aes_in=aes_in, aes_out=aes_out,
                      extra=dict(iv=w[:, 0, :], ciphertext=w,
                                 window=np.asarray(window, np.uint8),
                                 blocks=np.int64(BLOCKS),
                                 page=np.frombuffer(self.page, np.uint8)))

    def truth_key(self):
        return self._server_key

    def extra_arrays(self):
        # Also the partial-capture sidecar, so the known plaintext and the block
        # geometry have to be here: a salvaged TLS capture is the one that most
        # needs to be decodable, since its session cannot be resumed.
        out = {"blocks": np.int64(BLOCKS)}
        if self.page:
            # `page` is the descriptive name; `plaintext` is the one this demo's
            # analyse.py reads. Emitting both is what lets a generic capture go
            # into the existing analysis with no conversion step.
            out["page"] = np.frombuffer(self.page, np.uint8)
            out["plaintext"] = out["page"]
        if self._want:
            out["want"] = np.int64(self._want)
        if self._server_key:
            out["key"] = np.frombuffer(self._server_key, np.uint8)
            out["server_write_key"] = out["key"]
        if self.client_random:
            out["client_random"] = np.frombuffer(self.client_random, np.uint8)
        if self.server_random:
            out["server_random"] = np.frombuffer(self.server_random, np.uint8)
        return out
