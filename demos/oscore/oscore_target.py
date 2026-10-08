# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""OSCORE as an `ethtimer` target: the generic instrument pointed at this demo.

    python -m ethtimer.cli --target oscore --n 20000

The OSCORE client runs in Python ([`oscore_py.py`](oscore_py.py)) and the board
knows only bytes. That split is the point: the measurement is transmit-complete
interrupt to receive interrupt, the measured device's turnaround on the wire, and
the instrument has to stay out of the protocol to take it.

WHY THIS IS MODE_BANK AND SNMPV3 IS NOT. An OSCORE server keeps a replay window
(RFC 8613 s7.4) and retires each Partial IV it has seen, so a repeated request is
answered with an unprotected 4.00 rather than the reply this measures. There is
no varying input the victim supplies by itself: the response's CCM nonce is the
REQUEST's nonce, so if the request never changes, neither does the AES input --
and a capture of one AES input repeated 20 000 times says nothing. Every exchange
therefore needs a fresh request, which is what the instrument's request bank is
for.

THE PAIR THIS YIELDS. The response plaintext is `code | 0xFF | payload`, exactly
one 16-byte AES block and byte-identical on every response, so

    aes_in  = A_1 = 0x01 || nonce || 0x0001   -- the CCM counter block, derived
                                                 from the Partial IV the client
                                                 chose and sent in clear
    aes_out = plaintext ^ ciphertext[0:16]    -- E(K, A_1) under the SERVER's
                                                 sender key

Both sides are on the wire in clear or derivable from what is: no key is needed
to produce the pair, which is what makes the acceptance check a check rather
than a restatement. It also needs the client to be the side that remembers which
Partial IV went with which record, which is why it runs on the host.
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import oscore_py as O                                          # noqa: E402
from ethtimer.target import MODE_BANK, Blocks, Target, register   # noqa: E402

#: RFC 8613 Appendix C.1. The measured device is built with these, so the
#: "secret" is public and known to whoever set the bench up -- which is the only
#: reason the acceptance check can run at all.
MASTER_SECRET = bytes.fromhex("0102030405060708090a0b0c0d0e0f10")
MASTER_SALT = bytes.fromhex("9e7ca92223786340")
#: The client is the party with the empty Sender ID; the server answers as 0x01.
SENDER_ID = b""
RECIPIENT_ID = b"\x01"

#: `GET /t`, the single resource the victim serves.
URI_PATH = b"t"
#: The assumed response plaintext -- the publicly known shape of the resource's
#: reply, not a secret: `2.05 Content` then `{"v":22.54321}`. It must match the
#: measured device's actual response byte for byte; `prepare()` checks that it
#: does rather than assuming.
RESP_PLAINTEXT = bytes([0x45, 0xFF]) + b'{"v":22.54321}'
assert len(RESP_PLAINTEXT) == 16

WINDOW = 16


@register
class OscoreTarget(Target):
    name = "oscore"
    port = 5683
    mode = MODE_BANK
    timeout_ms = 200

    def __init__(self, uri_path=URI_PATH, start_ssn=0):
        self.client = O.Client(MASTER_SECRET, MASTER_SALT, SENDER_ID,
                               RECIPIENT_ID, uri_path=uri_path,
                               start_ssn=start_ssn)
        #: request bytes -> the Partial IV that built them. Requests are unique
        #: by construction (that is the whole point of a sequence number), so
        #: this is a safe key and it means the exchange->PIV map is established
        #: by what was actually SENT rather than by counting generated
        #: requests -- which the probe exchange and any replay-window retry
        #: both put out of step.
        self._piv_of = {}
        #: global exchange index -> Partial IV, filled by `note_sent`.
        self._exchange_piv = {}

    # ---- setup ----------------------------------------------------------
    #: How far to jump the sequence number when the victim rejects one as
    #: replayed. A window it has already passed is never reopened, so the only
    #: way forward is up; the stride is large enough that a handful of tries
    #: clears any plausible number of previous runs.
    SSN_STRIDE = 1 << 20

    def prepare(self, dev):
        """Find a sequence number the victim will accept, and check the reply.

        Worth its own exchange rather than discovering it inside a capture: a
        bank the victim rejects comes back as a batch of rejects, and at that
        point "the victim is not there", "the window has retired these numbers"
        and "the plaintext assumption is wrong" all read the same. Here each is
        named, one request in.

        THE REPLAY WINDOW IS NOT AN ERROR CONDITION. It is how OSCORE is
        supposed to behave, and a second run against a victim that has not been
        reset will always meet it: this client starts counting from zero and
        the victim has retired everything it saw last time. A window only ever
        moves forward, so the recovery is to move forward with it -- using
        nothing but the rejection the server already sends in clear.
        """
        for jump in range(6):
            for attempt in range(6):
                req, piv = self._next_request()
                rep = dev.oneshot(req)
                if not rep:
                    time.sleep(0.3)
                    continue
                if self._is_rejection(rep):
                    break                       # jump the sequence number
                try:
                    pt = self.client.open_response(rep, piv)
                except Exception as e:
                    raise RuntimeError(
                        "the victim answered %d bytes that do not open under "
                        "this context: %s. Check the victim is the RFC 8613 "
                        "C.1 build this target assumes." % (len(rep), e))
                if pt != RESP_PLAINTEXT:
                    raise RuntimeError(
                        "the victim's response plaintext is %r, not the %r "
                        "this target assumes. The assumed plaintext is what "
                        "turns the ciphertext into an AES output, so a capture "
                        "against the wrong one verifies nowhere."
                        % (pt, RESP_PLAINTEXT))
                print("oscore: victim answers, piv=%d, plaintext verified, "
                      "server key=%s" % (piv, self.client.recipient_key.hex()))
                return
            else:
                raise RuntimeError(
                    "the victim did not answer a protected GET on udp/5683 in "
                    "6 tries. Is it running, and is its IP the device's victim "
                    "address?")
            self.client.ssn += self.SSN_STRIDE
            print("oscore: the victim rejected piv=%d as replayed; it has seen "
                  "these before. Jumping to %d."
                  % (piv, self.client.ssn))
        raise RuntimeError(
            "the victim rejected every sequence number tried, up to %d. Its "
            "replay window should accept anything above what it has seen, so "
            "this is not simple exhaustion -- check the security context "
            "matches (master secret, salt, sender and recipient ids)."
            % self.client.ssn)

    @staticmethod
    def _is_rejection(rep):
        """An unprotected 4.00 Bad Request: RFC 8613 s8.2 steps 3 and 7.

        It is what the victim answers to a replayed Partial IV, to a request
        that fails verification, and to plain CoAP -- deliberately the same
        answer in all three cases, with no diagnostic payload, so the shape is
        all there is to go on: short, no payload marker, code 4.00.
        """
        return len(rep) >= 2 and rep[1] == 0x80 and 0xFF not in rep

    def _next_request(self):
        req, piv = self.client.build_request()
        self._piv_of[req] = piv
        return req, piv

    def requests(self):
        while True:
            yield self._next_request()[0]

    def note_sent(self, base, requests):
        for i, r in enumerate(requests):
            piv = self._piv_of.get(bytes(r))
            if piv is None:
                raise RuntimeError(
                    "exchange %d was sent a request this target did not build"
                    % (base + i))
            self._exchange_piv[base + i] = piv

    def window(self, reply):
        off = self.client.response_offset(reply)
        if len(reply) - off < WINDOW + O.TAG_LEN:
            raise RuntimeError(
                "the response payload is %d bytes, too short for a %d-byte "
                "block plus an %d-byte tag"
                % (len(reply) - off, WINDOW, O.TAG_LEN))
        return off, WINDOW

    # ---- results --------------------------------------------------------
    def blocks(self, window, index):
        ct = np.asarray(window, np.uint8)
        pt = np.frombuffer(RESP_PLAINTEXT, np.uint8)
        ks = ct ^ pt[None, :]

        # The AES input of a record is fixed by the Partial IV of the request
        # that produced it, and `note_sent` recorded exactly which that was.
        # An exchange with no entry is one this capture never sent -- it is
        # marked invalid rather than guessed at.
        a1 = np.zeros((len(ct), 16), np.uint8)
        pivs = np.full(len(ct), -1, np.int64)
        valid = np.zeros(len(ct), bool)
        for row, ex in enumerate(np.asarray(index, np.int64)):
            piv = self._exchange_piv.get(int(ex))
            if piv is None:
                continue
            n = O.nonce(self.client.common_iv, self.client.sender_id, piv)
            a1[row] = np.frombuffer(O.ccm_a1(n), np.uint8)
            pivs[row] = piv
            valid[row] = True

        return Blocks(aes_in=a1[:, None, :], aes_out=ks[:, None, :],
                      valid=valid,
                      extra=dict(keystream=ks, ciphertext=ct,
                                 plaintext=pt, piv=pivs))

    def truth_key(self):
        """The SERVER's sender key, for the acceptance check only.

        The client's recipient key by another name. Known here only because the
        master secret is the RFC's published test vector; a device configured
        with a secret of its own would return None and the capture would be
        reported as unchecked.
        """
        return self.client.recipient_key

    def extra_arrays(self):
        """Also the partial-capture sidecar. NOTE WHAT IS MISSING FROM IT.

        This target's AES input is derived from the Partial IV of the request
        that produced each record, which is PER-RECORD state built as the banks
        are uploaded. The sidecar is written once, before any of that exists, so
        a salvaged OSCORE capture carries its windows and timings but not the
        exchange-to-PIV map -- the keystreams are recoverable from it, the
        inputs are not. A clean finish saves the map as `piv`.

        This is a limitation, not an oversight: it is written down rather than
        papered over with a reconstruction that would be right only while
        nothing retried.
        """
        return dict(key=np.frombuffer(self.client.recipient_key, np.uint8),
                    common_iv=np.frombuffer(self.client.common_iv, np.uint8),
                    plaintext=np.frombuffer(RESP_PLAINTEXT, np.uint8),
                    sender_id=np.frombuffer(self.client.sender_id or bytes(1),
                                            np.uint8))
