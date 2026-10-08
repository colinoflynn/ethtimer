# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""The reference responder's wire format, mirrored from the firmware header.

`firmware/victim/inc/et_victim.h` is the definition; this is the host's copy of
it, and `tests/test_victim_proto.py` holds the two together the same way
`tests/test_proto.py` does for the instrument's own protocol. Two hand-kept
copies of one wire format is how the text protocol this project replaced came to
drift, and the fix there was a test that reads both.

Nothing here talks to a board. It builds request bytes and decodes reply bytes,
so it can be checked without hardware -- which is the only reason the field
offsets below can be trusted at all.
"""
from __future__ import annotations

import struct

import numpy as np

#: Bumped when a field moves. Checked in every decoded reply.
VERSION = 1

PORT = 7777

MAGIC_REQ = 0x51525445          # "ETRQ"
MAGIC_RSP = 0x52565445          # "ETVR"

REQ_HDR = 12
RSP_HDR = 32

CMD_MEASURE = 0
CMD_STATUS = 1

F_LINK_100F = 0x01
F_CYC_WRAP = 0x02

MAX_REPLY = 1472

#: Reply field offsets, as a table rather than as magic numbers in the decoder,
#: so the test can check each one against the header's own comment block.
RSP_FIELDS = {
    "magic":  (0, "<I"),
    "ver":    (4, "<B"),
    "flags":  (5, "<B"),
    "tag":    (8, "<I"),
    "seq":    (12, "<I"),
    "rx_cyc": (16, "<I"),
    "tx_cyc": (20, "<I"),
    "clk_hz": (24, "<I"),
    "n_seen": (28, "<I"),
}


def request(tag: int = 0, cmd: int = CMD_MEASURE, pad_to: int = 0) -> bytes:
    """Build one request.

    `pad_to` is the TOTAL UDP payload length, and the responder mirrors it, so
    one number sets the frame size in both directions. That is the whole
    frame-size control: a jitter measurement wants it swept, because
    serialisation is 80 ns per byte each way at 100 Mbit and a store-and-forward
    hop charges for the frame twice.
    """
    req = struct.pack("<IBBHI", MAGIC_REQ, VERSION, cmd, 0, tag & 0xFFFFFFFF)
    assert len(req) == REQ_HDR, "REQ_HDR disagrees with the packed header"
    if pad_to > len(req):
        if pad_to > MAX_REPLY:
            raise ValueError(
                "pad_to=%d exceeds the responder's %d-byte reply limit; a "
                "longer request would be answered at %d and the frame sizes "
                "would stop matching" % (pad_to, MAX_REPLY, MAX_REPLY))
        req += bytes(pad_to - len(req))
    return req


def decode(window: np.ndarray) -> dict:
    """Decode `(n, RSP_HDR)` uint8 of kept reply bytes into named arrays.

    Returns every reply field as its own `(n,)` array, plus:

    `victim_cyc`   -- `tx_cyc - rx_cyc`, the responder's own receive-interrupt
                      to send interval, in ITS cycles. Computed modulo 2**32 on
                      purpose: the counter is 32 bits and wraps every 23.9 s at
                      180 MHz, so the subtraction is right across a wrap and
                      only a reading of the raw fields would be wrong.
    `victim_us`    -- the same, in microseconds, using each record's own
                      `clk_hz`. Per record rather than once, because a capture
                      that spans a reflash of the responder would otherwise be
                      scaled by the wrong board's clock, silently.
    """
    w = np.asarray(window, np.uint8)
    if w.ndim != 2 or w.shape[1] < RSP_HDR:
        raise ValueError(
            "need (n, >=%d) uint8 of reply bytes, got %s -- the capture window "
            "has to cover the whole reply header" % (RSP_HDR, (w.shape,)))

    def u32(off):
        return w[:, off:off + 4].copy().view(np.uint32).reshape(-1)

    out = {
        "magic":  u32(0),
        "ver":    w[:, 4].astype(np.uint8),
        "flags":  w[:, 5].astype(np.uint8),
        "tag":    u32(8),
        "seq":    u32(12),
        "rx_cyc": u32(16),
        "tx_cyc": u32(20),
        "clk_hz": u32(24),
        "n_seen": u32(28),
    }
    out["victim_cyc"] = (out["tx_cyc"].astype(np.int64)
                         - out["rx_cyc"].astype(np.int64)) % (1 << 32)
    hz = out["clk_hz"].astype(np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        out["victim_us"] = np.where(hz > 0,
                                    out["victim_cyc"] / hz * 1e6,
                                    np.nan)
    return out


def check(dec: dict) -> None:
    """Raise unless every decoded record is a reply from this responder.

    This is the `dns` demo's question-section check in a different form, and it
    exists for the same reason: there is no key here, so nothing else
    distinguishes a correct capture from a window that landed four bytes off.
    The magic and the version are constants the responder puts in every frame,
    so a window that drifted fails here rather than producing a jitter
    histogram of garbage.
    """
    n = len(dec["magic"])
    bad = np.where(dec["magic"] != MAGIC_RSP)[0]
    if len(bad):
        raise RuntimeError(
            "%d of %d records do not begin with the responder's magic, first "
            "at record %d (got %#010x, expected %#010x). Every reply carries "
            "it, so this is the capture path -- a window offset, a record "
            "stride, or a frame that was not a reply to this request."
            % (len(bad), n, int(bad[0]), int(dec["magic"][bad[0]]), MAGIC_RSP))

    bad = np.where(dec["ver"] != VERSION)[0]
    if len(bad):
        raise RuntimeError(
            "%d of %d records report responder protocol v%d; this host speaks "
            "v%d. Reflash the responder, or use a host that matches it -- the "
            "timing fields are at different offsets between versions, so the "
            "numbers would be plausible and wrong."
            % (len(bad), n, int(dec["ver"][bad[0]]), VERSION))

    bad = np.where(dec["clk_hz"] == 0)[0]
    if len(bad):
        raise RuntimeError(
            "%d of %d records report a responder clock of 0 Hz, first at "
            "record %d. Its own interval cannot be converted to microseconds, "
            "so the path figure would be the round trip with nothing taken "
            "off." % (len(bad), n, int(bad[0])))


def continuity(dec: dict) -> dict:
    """What the responder's own counter says about the path, as a dict.

    THE INSTRUMENT CANNOT SEE THIS. It counts timeouts, which says an exchange
    produced no reply; it cannot say that reply 400 arrived after reply 401,
    because from its side both are just the next datagram. The responder's `seq`
    can, and through a loaded switch reordering is a thing that happens.

    `gaps`      exchanges where seq advanced by more than 1: a reply the
                responder sent and the host never saw, or one it never sent.
    `backwards` exchanges where seq went down: reordering, which no timeout
                count would ever have shown.
    `missing`   how many replies the gaps account for.
    """
    seq = dec["seq"].astype(np.int64)
    if len(seq) < 2:
        return dict(gaps=0, backwards=0, missing=0, first=int(seq[0]) if len(seq) else 0,
                    last=int(seq[-1]) if len(seq) else 0)
    d = np.diff(seq)
    return dict(
        gaps=int((d > 1).sum()),
        backwards=int((d < 1).sum()),
        missing=int(d[d > 1].sum() - (d > 1).sum()),
        first=int(seq[0]),
        last=int(seq[-1]),
    )
