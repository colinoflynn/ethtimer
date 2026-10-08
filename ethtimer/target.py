# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""What a protocol adapter has to supply, and nothing else.

`Device` speaks UDP and TCP and knows the word "bytes". Everything above that --
what a request means, where the ciphertext sits in a reply, which AES block the
captured bytes correspond to -- is a `Target`, and targets live in `demos/`, not
in this package. The instrument stays protocol-agnostic; see the module
docstring in `campaign.py` for why that boundary is worth the indirection.

THE ONE THING EVERY TARGET HAS IN COMMON. Every protocol `demos/` covers puts an
AES *input* on the wire in clear as well as an AES *output*:

| protocol | AES input | AES output |
|---|---|---|
| SNMPv3 (CFB) | `C1`, the first ciphertext block, in clear | `ks2 = P2 ^ C2` |
| OSCORE (CCM/CTR) | the counter block, derived from the public Partial IV | `ks = P ^ C` |
| TLS 1.2 (CBC) | `P_i ^ C_(i-1)`, and the explicit IV is in clear | `C_i` |

So `blocks()` returns both, and the acceptance check is the same sentence for
every protocol: encrypt the input under the victim's real key and see whether
the output comes back. That is what makes this framework generic rather than
three scripts in a trenchcoat -- and it is a stronger check than a record count,
because it exercises the request, the reply parse, the window offset and the
transfer at once.

The real key is used ONLY by that check. No adapter may consult it when
producing `blocks()`: the capture has to stand on what is observable on the
wire, and an adapter that reaches for the key makes the check tautological.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional, Sequence

import numpy as np

from . import proto as P

#: `requests()` yields one request per exchange, forever. A target whose victim
#: accepts a repeated datagram yields the same bytes every time and is told so by
#: `mode == "fixed"`, which lets the device hold the request and run a whole
#: batch without the host in the loop.
MODE_FIXED = "fixed"
#: The victim rejects repeats -- an OSCORE replay window, a sequence number, a
#: nonce it will not see twice. The host uploads a bank of distinct requests and
#: the device plays it once. Costs an upload per batch.
MODE_BANK = "bank"
#: One exchange per host round trip, on a held-open connection. What TLS needs,
#: because every record carries a sequence number and cannot be pre-generated.
MODE_RELAY = "relay"

MODES = (MODE_FIXED, MODE_BANK, MODE_RELAY)


@dataclass
class Blocks:
    """The AES pairs one capture yielded, plus whatever the adapter wants kept.

    `aes_in` and `aes_out` are `(n, k, 16)` uint8: `n` exchanges, `k` usable AES
    blocks per exchange. `k` is 1 for SNMPv3 and OSCORE and 15 for TLS, where the
    victim encrypted a whole 255-byte record under one measured turnaround -- and
    using all fifteen is the difference between a 13-hour capture and a one-hour
    one, so `k > 1` is a first-class case rather than an afterthought.

    A row of `valid` that is False means that exchange produced no usable block
    (a reply that parsed but held nothing, say). Timing is still recorded for it;
    `campaign` drops it before saving and says how many went.
    """
    aes_in: np.ndarray
    aes_out: np.ndarray
    valid: Optional[np.ndarray] = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.aes_in.shape != self.aes_out.shape:
            raise ValueError("aes_in %s and aes_out %s must have the same shape"
                             % (self.aes_in.shape, self.aes_out.shape))
        if self.aes_in.ndim != 3 or self.aes_in.shape[2] != 16:
            raise ValueError("aes_in must be (n, k, 16), got %s"
                             % (self.aes_in.shape,))
        if self.valid is None:
            self.valid = np.ones(len(self.aes_in), bool)

    @property
    def n(self) -> int:
        return len(self.aes_in)

    @property
    def per_exchange(self) -> int:
        return self.aes_in.shape[1]


class Target:
    """Subclass this, put it next to the demo it belongs to, and register it.

    The minimum is `port`, `prepare()`, `requests()`, `window()` and `blocks()`.
    Everything else has a default that suits a fixed-request UDP protocol.
    """

    #: Shown in output and stored in the capture. Keep it short.
    name = "unnamed"
    #: UDP or TCP port on the victim.
    port = 0
    #: One of MODE_FIXED / MODE_BANK / MODE_RELAY.
    mode = MODE_FIXED
    #: P.PROTO_UDP or P.PROTO_TCP.
    transport = P.PROTO_UDP
    #: Per-exchange reply timeout the device enforces.
    timeout_ms = 200

    # ---- setup ----------------------------------------------------------
    def prepare(self, dev) -> None:
        """Talk to the victim until you know what the requests need.

        Called with the device already addressed at the victim, so `dev.oneshot`
        works. This is where an engine discovery, a handshake or a key
        derivation goes. Raise to abort the campaign with a readable message.
        """

    def requests(self) -> Iterator[bytes]:
        """Yield one request per exchange, indefinitely.

        A MODE_FIXED target yields the same bytes forever and only the first is
        ever used. A MODE_BANK target must yield a *fresh* request every time --
        if it repeats, the victim's replay window rejects the repeat and the
        batch comes back as timeouts, which is the failure this mode exists to
        avoid.
        """
        raise NotImplementedError

    def sample_reply(self):
        """A real reply already in hand, or None to let `campaign` fetch one.

        `campaign` normally sends one exchange before a batch, to warm ARP and
        to get a reply to size the window against. A target whose `prepare()`
        already completed a real exchange returns it here instead, and that
        probe is skipped.

        TCP targets must return one. The probe is a UDP `oneshot`, which a
        stream transport has no equivalent of -- and for TLS the probe would
        also burn a record from a write state the bank is about to continue.
        """
        return None

    def window(self, reply: bytes) -> tuple:
        """`(offset, length)` of the slice worth keeping, given a sample reply.

        Taking a real reply rather than a constant is deliberate: the SNMPv3
        ciphertext offset moves with the BER length of engineTime, so a constant
        here was wrong on the day the victim's uptime crossed 2^7 ticks.
        """
        raise NotImplementedError

    def note_sent(self, base: int, requests) -> None:
        """Told which requests became which exchanges, before they are sent.

        `requests[i]` is the request for global exchange `base + i`. Called once
        per bank for a MODE_BANK target, and once with a single request for a
        MODE_FIXED one (every exchange uses it).

        An adapter whose AES input depends on what it SENT needs this, and must
        not try to infer it by counting its own generator: the probe exchange
        that sizes the window consumes a request, a MODE_BANK capture then
        discards that one rather than replaying it, and any retry inside
        `prepare()` consumes more. Counting gets all three wrong in different
        directions, and the result is a capture whose every record is paired
        with a neighbouring AES input -- which does not fail loudly, it just
        verifies nowhere.
        """

    def between_batches(self, dev):
        """Called after each device batch. Return a new window, or None.

        A capture is several batches, and some protocols move the bytes it is
        keeping BETWEEN them. SNMPv3 does: the reply carries `engineTime` as a
        BER INTEGER ahead of the ciphertext, so when uptime crosses 127 seconds
        the length grows by a byte and everything after it shifts. Measured on
        this bench: a 20 000-exchange capture verified for 5 956 records and
        then failed every one after, because the window was set once from the
        first reply and the victim crossed that boundary eleven seconds in.

        The encrypted plaintext does not change -- engineTime is in the header,
        outside it -- so only the OFFSET moves, and a capture that re-reads it
        is correct across the boundary rather than half wrong.

        Return `(off, len)` or `(off, len, want)` to move the window, or None
        to leave it. Returning None costs nothing; a target whose offset cannot
        move should not probe for one.
        """
        return None

    # ---- results --------------------------------------------------------
    def blocks(self, window: np.ndarray, index: np.ndarray) -> Blocks:
        """Turn the kept bytes into AES (input, output) pairs.

        `window` is `(n, win_len)` uint8, exactly the bytes the device kept.
        `index` is `(n,)` int: for each record, WHICH exchange of the whole
        capture produced it, counted from zero.

        `index` is not `arange(n)`. A timed-out or short exchange leaves no
        record but still consumes a request, so the k-th record is generally not
        the k-th request -- and for a MODE_BANK target, whose AES input is
        derived from the sequence number of the request that was actually sent,
        assuming otherwise silently pairs every record with the wrong input. The
        device reports its own per-batch `seq` and `campaign` turns that into
        this global index, so the adapter never has to reconstruct it.

        Must not use the real key.
        """
        raise NotImplementedError

    def truth_key(self) -> Optional[bytes]:
        """The victim's real key, for the acceptance check ONLY, or None.

        Returning None is allowed and means the capture cannot be checked
        against ground truth; `campaign` says so loudly rather than passing
        quietly, because an unverifiable capture is how a window-offset bug
        survives to the analysis stage.
        """
        return None

    def extra_arrays(self) -> dict:
        """Anything else worth saving beside the capture, as name -> array."""
        return {}

    # ---- relay only -----------------------------------------------------
    def relay_step(self, dev, i: int):
        """One exchange in MODE_RELAY. Return the bytes to keep, or None.

        Only called for MODE_RELAY targets, where the host is in the loop for
        every exchange and the adapter drives it: build a record, call
        `dev.relay()`, feed the reply back into whatever state the protocol
        keeps. Return the window bytes for this exchange, or None to skip it.
        """
        raise NotImplementedError


# ------------------------------------------------------------------ registry --

_REGISTRY = {}


def register(cls):
    """Class decorator: make a target loadable by name from the CLI."""
    _REGISTRY[cls.name] = cls
    return cls


def get(name: str):
    if name not in _REGISTRY:
        raise KeyError("no target named %r; known: %s"
                       % (name, ", ".join(sorted(_REGISTRY)) or "(none loaded)"))
    return _REGISTRY[name]


def known() -> Sequence[str]:
    return tuple(sorted(_REGISTRY))
