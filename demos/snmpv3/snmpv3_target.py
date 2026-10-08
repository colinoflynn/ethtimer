# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""SNMPv3 as an `ethtimer` target: the generic instrument pointed at this demo.

    python -m ethtimer.cli --target snmpv3 --n 20000

This is the adapter and nothing else. It says what an SNMPv3 request looks
like, where the ciphertext sits in the reply, and which AES pair the captured
bytes form; the capture loop, the batching, the diagnostics and the acceptance
check all live in `ethtimer.campaign` and are shared with the other protocols.

[`README.md`](README.md) says what the measured device has to be running.

WHY MODE_FIXED WORKS HERE. A fixed request is enough whenever the victim
supplies the varying AES input itself, and SNMPv3 does: the CFB IV is
`engineBoots || engineTime || privParam`, and engineTime ticks, so the same
request datagram produces a different IV -- and therefore a different `C1`, the
AES input -- on every exchange. It is also why the ratio of exchanges to
distinct AES inputs is a property of the victim's clock and not of this
adapter.

THE PAIR THIS YIELDS. RFC 3826 is AES-128-CFB128, so keystream block `n` is
`E(K, C(n-1))`. The window holds `C1` and `C2`; the plaintext is standard MIB-2
BER and is byte-identical on every response, so `ks2 = P2 ^ C2` is an AES output
whose input `C1` is in clear on the wire.

    aes_in  = C1
    aes_out = ks2 = P2 ^ C2
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import snmp3                                                    # noqa: E402
from ethtimer.target import MODE_FIXED, Blocks, Target, register  # noqa: E402

#: The agent's credentials. The host uses the same ones, because the whole
#: measurement is of a device answering a request it considers legitimate -- an
#: SNMPv3 agent does not encrypt a reply to anyone it has not authenticated, so
#: there is no exchange to time without them. See README.md § *What the measured
#: device has to be*.
USER = b"victim"
AUTH_PASS = b"authpassword"
PRIV_PASS = b"privpassword"
#: lwIP's agent localizes with SHA-1. The Harmony agent uses MD5, which is the
#: whole difference between the two adapters' `prepare()`.
ALGO = "sha"

SYS_DESCR = (1, 3, 6, 1, 2, 1, 1, 1, 0)
#: Two AES blocks: C1 is the input, C2 gives the output once XORed with the
#: known plaintext.
WINDOW = 32

#: Bytes kept BEFORE the ciphertext, and after the block pair.
#:
#: THE WINDOW IDENTIFIES ITS OWN ALIGNMENT. The ciphertext is a BER OCTET
#: STRING, so it is preceded by a tag-and-length header -- `04 82 00 48` on this
#: victim -- that is fixed, public and nothing to do with the key. Keeping it
#: lets `blocks()` find where the ciphertext really starts IN EACH RECORD, which
#: matters because the offset moves *during* a capture: `engineTime` sits ahead
#: of it as a BER INTEGER and grows by a byte when uptime crosses 127 seconds.
#:
#: Re-reading the offset between batches (`between_batches`) is not enough on
#: its own. Measured here: the offset moved at record 11 802 and the batch ran
#: to 11 912, so 110 records were captured against the stale offset -- 0.55 % of
#: the capture, silently wrong, with no timeouts and no short replies. With the
#: header in the window those records are realigned instead of lost.
LEAD = 6
SLACK = 2


@register
class Snmpv3Target(Target):
    name = "snmpv3"
    port = 161
    mode = MODE_FIXED
    timeout_ms = 200

    def __init__(self, user=USER, auth_pass=AUTH_PASS, priv_pass=PRIV_PASS,
                 algo=ALGO, oid=SYS_DESCR):
        self.user, self.algo, self.oid = user, algo, oid
        self._auth_pass, self._priv_pass = auth_pass, priv_pass
        self._priv = None
        self._auth = None
        #: msgID may vary freely -- it is in the cleartext header. request_id
        #: may NOT: it sits inside the scopedPDU, so changing it changes the
        #: response plaintext and breaks `keystream = P ^ C`.
        self._msg_id = 1000
        self._req = None
        self._plain = None
        self._engine = None
        #: The BER header that marks where the ciphertext starts. Read off a
        #: real reply rather than hardcoded, so a victim with a different
        #: ciphertext length works without an edit.
        self._marker = None

    # ---- setup ----------------------------------------------------------
    def prepare(self, dev):
        """Discover the engine, localize the keys, build the one request.

        The engine id is not knowable in advance: it is derived from the
        victim's MAC, and localization binds the passphrase to it, so this
        dialogue has to happen against the live victim every run.
        """
        rep = b""
        for _ in range(10):
            rep = dev.oneshot(snmp3.build_discovery())
            if rep:
                break
            time.sleep(0.4)
        if not rep:
            raise RuntimeError("the victim did not answer engine discovery on "
                               "udp/161 -- is the SNMPv3 agent running?")
        m = snmp3.parse_message(rep)
        self._engine = m["engine_id"]
        self._auth = snmp3.password_to_key(self._auth_pass, self._engine,
                                           self.algo)[:20]
        self._priv = snmp3.password_to_key(self._priv_pass, self._engine,
                                           self.algo)[:16]
        print("engine=%s  priv=%s" % (self._engine.hex(), self._priv.hex()))
        self._build(m)

    def _build(self, m):
        """One authPriv GET, stamped with the agent's CURRENT time.

        Rebuilt periodically, not once: RFC 3414 s3.2 bounds
        `msgAuthoritativeEngineTime` to +/-150 seconds of the agent's clock, and
        any capture worth taking outlives that. A request that has aged out is
        not refused -- the agent answers it with an unencrypted usmStats REPORT,
        which arrives on time, at the right length, and carries no ciphertext at
        all. Measured: a 250 000-exchange capture returned 75 534 usable records
        and 174 466 reports, having aged out 138 seconds in.
        """
        self._msg_id += 1
        self._req, _ = snmp3.build_message(m["engine_id"], m["boots"], m["time"],
                                           self.user, self._auth, self._priv,
                                           self._msg_id, 1000, [self.oid],
                                           algo=self.algo)
        return self._req

    def requests(self):
        while True:
            yield self._req

    def window(self, reply):
        """Where the ciphertext starts, read off THIS reply.

        Not a constant: engineTime's BER length grows by a byte when the
        victim's uptime crosses a power of two, and that moves everything after
        it. A constant here was wrong on exactly that day.
        """
        off = snmp3.ciphertext_offset(reply)
        # The plaintext is the same on every response, so one decrypt fixes it
        # for the whole capture -- but it must be decrypted from a reply, since
        # the boots/time in the IV are the victim's.
        r = snmp3.parse_message(reply)
        iv = snmp3.cfb_iv(r["boots"], r["time"], r["priv_param"])
        self._plain = snmp3.cfb_decrypt(self._priv, iv, r["body"])[:WINDOW]
        if len(self._plain) < WINDOW:
            raise RuntimeError("the reply's ciphertext is %d bytes, less than "
                               "the %d-byte window" % (len(self._plain), WINDOW))
        self._marker = bytes(reply[off - 4:off])
        if len(self._marker) != 4 or self._marker[0] != 0x04:
            raise RuntimeError(
                "expected a BER OCTET STRING header before the ciphertext at "
                "offset %d, found %r. The window uses it to realign records "
                "captured while the offset was moving." % (off, self._marker))
        start = max(0, off - LEAD)
        return start, LEAD + WINDOW + SLACK

    def between_batches(self, dev):
        """Re-stamp the request, and re-read the ciphertext offset.

        TWO things go stale during a capture, and they are unrelated.

        `engineTime` sits ahead of the ciphertext as a BER INTEGER, so its
        length grows by a byte when the victim's uptime crosses 127 seconds --
        and again at 32 767 -- and everything after it shifts. A 20 000-exchange
        capture on this bench verified for 5 956 records and failed every one
        after, because the victim crossed 127 s eleven seconds into the run.

        One probe exchange per batch, which is a few hundredths of a percent of
        a batch. The plaintext is unaffected by either: engineTime is in the
        header, outside the encrypted scoped PDU, and request_id is held fixed
        across rebuilds -- so only the offset moves and records either side are
        equally good. That the plaintext really did not change is checked, not
        assumed, because mixing two encodings in one capture is not something a
        record count would show.
        """
        disc = dev.oneshot(snmp3.build_discovery())
        if not disc:
            return None
        try:
            m = snmp3.parse_message(disc)
        except Exception:
            return None
        dev.set_request(self._build(m))

        rep = dev.oneshot(self._req)
        if not rep:
            return None
        try:
            off = snmp3.ciphertext_offset(rep)
            r = snmp3.parse_message(rep)
            iv = snmp3.cfb_iv(r["boots"], r["time"], r["priv_param"])
            plain = snmp3.cfb_decrypt(self._priv, iv, r["body"])[:WINDOW]
        except Exception:
            return None
        if plain != self._plain:
            raise RuntimeError(
                "the response plaintext changed when the request was re-stamped "
                "-- the capture would mix two encodings and `keystream = P ^ C` "
                "would be wrong for one of them. request_id must stay fixed "
                "across rebuilds; msgID may vary.")
        return max(0, off - LEAD), LEAD + WINDOW + SLACK

    # ---- results --------------------------------------------------------
    def blocks(self, window, index):
        raw = np.asarray(window, np.uint8)
        mark = np.frombuffer(self._marker, np.uint8)

        # Find the OCTET STRING header in each record and take the ciphertext
        # from just after it. A record whose header is not where the last one's
        # was is one the offset moved under, and it is realigned rather than
        # dropped or -- worse -- kept one byte out.
        n, w = raw.shape
        ct = np.zeros((n, WINDOW), np.uint8)
        valid = np.zeros(n, bool)
        shift = np.full(n, -1, np.int8)
        for s in range(0, w - len(mark) - WINDOW + 1):
            hit = (raw[:, s:s + 4] == mark[None, :]).all(axis=1) & ~valid
            if not hit.any():
                continue
            ct[hit] = raw[np.ix_(hit.nonzero()[0],
                                 np.arange(s + 4, s + 4 + WINDOW))]
            shift[hit] = s
            valid |= hit

        pt = np.frombuffer(self._plain, np.uint8)
        ks = ct ^ pt[None, :]
        aes_in = ct[:, 0:16][:, None, :]          # C1, in clear on the wire
        aes_out = ks[:, 16:32][:, None, :]        # ks2 = E(K, C1)
        # `keystream`/`keystream2` are the names this demo's `analyse.py` reads.
        # Emitting them as well as the generic `aes_in`/`aes_out` means a
        # capture taken with the new instrument goes straight into the existing
        # analysis with no shim and no conversion step -- which is most of what
        # "use the generic tool" has to mean to be worth doing.
        return Blocks(aes_in=aes_in, aes_out=aes_out, valid=valid,
                      extra=dict(keystream=ks[:, 0:16],
                                 keystream2=ks[:, 16:32],
                                 ciphertext=ct,
                                 raw_window=raw,
                                 align=shift,
                                 plaintext=pt,
                                 engine_id=np.frombuffer(self._engine, np.uint8)))

    def truth_key(self):
        return self._priv

    def extra_arrays(self):
        """Everything a salvaged capture needs to decode, plus the truth key.

        This is what goes into the partial-capture sidecar, which is written
        once before the run -- so anything needed to turn kept bytes back into
        AES pairs has to be here and not only in `blocks()`. For this target
        that is the known plaintext and the BER header the window realigns on;
        both are constants of the run.
        """
        return dict(key=np.frombuffer(self._priv, np.uint8),
                    plaintext=np.frombuffer(self._plain, np.uint8),
                    marker=np.frombuffer(self._marker, np.uint8),
                    lead=np.int64(LEAD), window_blocks=np.int64(WINDOW))
