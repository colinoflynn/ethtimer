# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""Build password-check requests and read what comes back.

The reply is the ordinary responder reply -- same magic, same 32-byte header,
decoded by [`../jitter/etv.py`](../jitter/etv.py) -- with one extra flag bit
saying whether the guess was right. **That bit is all the device tells you.**
Everything else a capture learns about the secret is in `tx_cyc - rx_cyc`.

See `../../firmware/common/pwcheck.h` for the two comparisons on the device: one
that returns at the first wrong byte, and one that does not.
"""
from __future__ import annotations

import os
import struct
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_JITTER = os.path.join(os.path.dirname(_HERE), "jitter")
if _JITTER not in sys.path:
    sys.path.insert(0, _JITTER)

import etv                                                     # noqa: E402

#: Mirrored from firmware/common/pwcheck.h and firmware/victim/inc/et_victim.h.
#: `tests/test_password_proto.py` holds them together.
CMD_PWCHECK = 2          # the leaky comparison
CMD_PWCHECK_CT = 3       # the constant-time control
PW_OFF = 12              # guess_len
PW_GUESS_OFF = 14        # the guess itself
PW_MAX_GUESS = 32

F_PW_MATCH = 0x04
F_PW_BAD_REQ = 0x08

#: The device's compile-time secret, so a recovered answer can be checked. It is
#: not a secret in any sense that matters: it is in the firmware source, and the
#: whole demo is about getting it out WITHOUT being told.
SECRET = b"hunter2!"


def request(guess: bytes, tag: int = 0, const_time: bool = False,
            pad_to: int = 0) -> bytes:
    """One password-check request.

    `pad_to` is the TOTAL payload length, as everywhere else here, so a guess can
    be sent in a frame of any size. **Keep it fixed across a sweep**: the frame
    size changes the serialisation time by 80 ns a byte each way, which is bigger
    than the signal, and a sweep in which the frame grew with the guess would
    measure the frame.
    """
    if len(guess) > PW_MAX_GUESS:
        raise ValueError("guess is %d bytes; the device accepts at most %d"
                         % (len(guess), PW_MAX_GUESS))
    cmd = CMD_PWCHECK_CT if const_time else CMD_PWCHECK
    req = bytearray(struct.pack("<IBBHI", etv.MAGIC_REQ, etv.VERSION, cmd, 0,
                                tag & 0xFFFFFFFF))
    assert len(req) == PW_OFF, "PW_OFF disagrees with the packed header"
    req.append(len(guess))
    req.append(0)                                   # reserved
    assert len(req) == PW_GUESS_OFF
    req += guess

    need = max(pad_to, len(req))
    if need > etv.MAX_REPLY:
        raise ValueError("pad_to=%d exceeds the responder's %d-byte limit"
                         % (need, etv.MAX_REPLY))
    req += bytes(need - len(req))
    return bytes(req)


def decode(window: np.ndarray) -> dict:
    """The ordinary reply decode, plus the two password flags."""
    d = etv.decode(window)
    etv.check(d)
    d["pw_match"] = (d["flags"] & F_PW_MATCH).astype(bool)
    d["pw_bad_req"] = (d["flags"] & F_PW_BAD_REQ).astype(bool)
    return d


def usable(d: dict) -> np.ndarray:
    """Records whose timing means something.

    A request the device could not parse as a guess is flagged and no check was
    run, so its interval is the baseline rather than a measurement. Averaging it
    in would pull every candidate towards the same number -- the failure mode
    where a sweep comes out flat and gets read as "constant time".
    """
    return ~d["pw_bad_req"]


#: A record more than this many cycles above the group median did something
#: other than the comparison. Thirty-two cycles is 178 ns at 180 MHz -- three
#: times the per-byte step, so nothing the comparison does can reach it.
EXCURSION_CYC = 32


def mean_us(d: dict) -> float:
    """Mean responder interval over the usable records, in microseconds."""
    u = usable(d)
    if not u.any():
        return float("nan")
    return float(np.mean(d["victim_us"][u]))


def sem_us(d: dict) -> float:
    """Standard error of that mean."""
    u = usable(d)
    n = int(u.sum())
    if n < 2:
        return float("inf")
    return float(np.std(d["victim_us"][u], ddof=1) / np.sqrt(n))


def median_cyc(d: dict) -> float:
    """MEDIAN responder interval, in its own cycles. **Use this, not the mean.**

    The per-record value is very nearly deterministic: measured on a NUCLEO-F429ZI
    against the bare-metal responder, a wrong byte is 519 cycles in 199 records
    out of 200 and a correct one is 530 in 198 out of 200. The step is 11 cycles.

    What is NOT Gaussian is the tail. A small and variable fraction of exchanges
    -- nine in two hundred in one group measured here -- come in about 90 cycles
    high, half a microsecond, from something on the responder that is not the
    comparison. Nine such records move a 200-record MEAN by 4 cycles, which is a
    third of the signal, and that is enough to put a wrong byte above the right
    one. It did: a scan on the mean picked the correct first byte and then a
    wrong second one, from a candidate whose median was the floor.

    The median does not care. With a value this concentrated it is exact for any
    group where fewer than half the records are excursions, which makes the
    required group size a handful rather than a calculation.
    """
    u = usable(d)
    if not u.any():
        return float("nan")
    return float(np.median(d["victim_cyc"][u].astype(np.float64)))


def median_us(d: dict) -> float:
    """The same, in microseconds, using each record's own reported clock."""
    u = usable(d)
    if not u.any():
        return float("nan")
    hz = d["clk_hz"][u].astype(np.float64)
    return float(np.median(d["victim_cyc"][u].astype(np.float64) / hz * 1e6))


def excursions(d: dict) -> int:
    """Records more than EXCURSION_CYC above the group median.

    Reported rather than silently dropped: their number is a property of the
    responder worth seeing, and a group that is mostly excursions is a group
    whose median means nothing either.
    """
    u = usable(d)
    if not u.any():
        return 0
    c = d["victim_cyc"][u].astype(np.float64)
    return int((c > np.median(c) + EXCURSION_CYC).sum())


def trimmed_mean(x: np.ndarray, thresh: float) -> float:
    """Mean of `x` after dropping everything more than `thresh` above its median.

    THE ESTIMATOR THAT WORKS ON BOTH CLOCKS, and the two fail differently.

    The responder's own interval is nearly deterministic, so its median is exact
    and a mean is only poisoned by the excursion tail. Trimming removes the tail
    and the mean then agrees with the median.

    The ROUND TRIP is different: it is quantised. The reply leaves the MAC
    aligned to the RMII clock, so a 61 ns delay inside the responder shows up at
    the far end as either nothing or a whole byte-time of 80 ns -- measured, the
    round trip moves in ~80 ns jumps every second byte rather than smoothly every
    byte. A median of that lands on a grid point and adjacent candidates TIE: a
    scan on the round-trip median returned eight bytes with "+0.0000 us clear of
    the runner-up" and recovered nothing.

    A mean does not tie, because quantisation noise averages away -- that is what
    quantisation noise does. So: trim the tail, then average. One estimator, both
    clocks, and the reason it is not just "the median" is written down here.
    """
    if len(x) == 0:
        return float("nan")
    keep = x <= (np.median(x) + thresh)
    if not keep.any():
        return float(np.median(x))
    return float(np.mean(x[keep]))


def modal_fraction(d: dict) -> float:
    """What share of records sit on the single commonest cycle count.

    The honest replacement for a standard deviation here: the distribution is a
    spike plus a tail, and "99 % of records read exactly 519" says more about how
    many exchanges a scan needs than any sigma does.
    """
    u = usable(d)
    if not u.any():
        return float("nan")
    c = d["victim_cyc"][u]
    _, counts = np.unique(c, return_counts=True)
    return float(counts.max()) / float(len(c))
