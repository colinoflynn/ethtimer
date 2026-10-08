# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""Hold the host protocol and the firmware header together.

No hardware and no toolchain: this parses `firmware/inc/ethtimer.h` and checks
every constant the host mirrors still matches it. A protocol change that updates
one side and forgets the other fails here rather than at the bench, which is the
only place the old text protocol's drift ever showed up.

    python -m pytest -q
"""
from __future__ import annotations

import os
import re
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)                           # the repository root

from ethtimer import proto as P                    # noqa: E402

HEADER = os.path.join(_ROOT, "firmware", "inc", "ethtimer.h")


def _defines():
    """Every simple `#define NAME <int>` in the firmware header."""
    out = {}
    pat = re.compile(r"^#define\s+(ET_[A-Z0-9_]+)\s+(0x[0-9A-Fa-f]+|\d+)u?\b")
    with open(HEADER, "r", encoding="utf-8") as fh:
        for line in fh:
            m = pat.match(line.strip())
            if m:
                out[m.group(1)] = int(m.group(2), 0)
    return out


PAIRS = [
    ("ET_PROTO_VERSION", "PROTO_VERSION"),
    ("ET_MAX_REQ", "MAX_REQ"),
    ("ET_MAX_WIN", "MAX_WIN"),
    ("ET_REC_OVERHEAD", "REC_OVERHEAD"),
    ("ET_CMD_PING", "CMD_PING"),
    ("ET_CMD_GET_INFO", "CMD_GET_INFO"),
    ("ET_CMD_SET_NET", "CMD_SET_NET"),
    ("ET_CMD_SET_TARGET", "CMD_SET_TARGET"),
    ("ET_CMD_SET_REQUEST", "CMD_SET_REQUEST"),
    ("ET_CMD_SET_WINDOW", "CMD_SET_WINDOW"),
    ("ET_CMD_GET_CONFIG", "CMD_GET_CONFIG"),
    ("ET_CMD_RUN", "CMD_RUN"),
    ("ET_CMD_ONESHOT", "CMD_ONESHOT"),
    ("ET_CMD_RESET", "CMD_RESET"),
    ("ET_RSP_ACK", "RSP_ACK"),
    ("ET_RSP_ERR", "RSP_ERR"),
    ("ET_RSP_INFO", "RSP_INFO"),
    ("ET_RSP_CONFIG", "RSP_CONFIG"),
    ("ET_RSP_BATCH_HDR", "RSP_BATCH_HDR"),
    ("ET_RSP_BATCH_DATA", "RSP_BATCH_DATA"),
    ("ET_RSP_BATCH_END", "RSP_BATCH_END"),
    ("ET_RSP_ONESHOT", "RSP_ONESHOT"),
    ("ET_ERR_NONE", "ERR_NONE"),
    ("ET_ERR_BAD_LEN", "ERR_BAD_LEN"),
    ("ET_ERR_BAD_ARG", "ERR_BAD_ARG"),
    ("ET_ERR_NO_REQUEST", "ERR_NO_REQUEST"),
    ("ET_ERR_LINK_DOWN", "ERR_LINK_DOWN"),
    ("ET_ERR_NOT_IMPL", "ERR_NOT_IMPL"),
    ("ET_ERR_TOO_BIG", "ERR_TOO_BIG"),
    ("ET_ERR_WINDOW", "ERR_WINDOW"),
    ("ET_PROTO_UDP", "PROTO_UDP"),
    ("ET_PROTO_TCP", "PROTO_TCP"),
    ("ET_LINK_DOWN", "LINK_DOWN"),
    ("ET_LINK_10H", "LINK_10H"),
    ("ET_LINK_10F", "LINK_10F"),
    ("ET_LINK_100H", "LINK_100H"),
    ("ET_LINK_100F", "LINK_100F"),
    # v2
    ("ET_CMD_SET_BANK", "CMD_SET_BANK"),
    ("ET_CMD_TCP_CONNECT", "CMD_TCP_CONNECT"),
    ("ET_CMD_TCP_CLOSE", "CMD_TCP_CLOSE"),
    ("ET_CMD_RELAY", "CMD_RELAY"),
    ("ET_RSP_RELAY", "RSP_RELAY"),
    ("ET_ERR_TCP", "ERR_TCP"),
    ("ET_ERR_BANK", "ERR_BANK"),
    ("ET_BANK_RESET", "BANK_RESET"),
    ("ET_MAX_FRAME_IN", "MAX_FRAME_IN"),
]

#: Prefixes whose every member must be mirrored on the host. The explicit PAIRS
#: table above is what the values are checked against; this is what stops the
#: table itself from going stale.
MIRRORED_PREFIXES = ("ET_CMD_", "ET_RSP_", "ET_ERR_", "ET_PROTO_", "ET_LINK_")

#: Header constants that are deliberately NOT mirrored, with the reason. Adding
#: a name here is a decision; forgetting one is caught by the test below.
NOT_MIRRORED = {
    # Sizes the host reads out of GET_INFO instead, precisely so that it never
    # has to be rebuilt to match a board.
    "ET_MAX_REPLY": "per-board, reported in INFO",
    "ET_RING_BYTES": "per-board, reported in INFO",
    "ET_BANK_BYTES": "per-board, reported in INFO",
    "ET_BANK_MAX": "per-board, reported in INFO",
    "ET_MAGIC0": "spelt as the bytes b'ET' on the host",
    "ET_MAGIC1": "spelt as the bytes b'ET' on the host",
    "ET_FW_VERSION": "the firmware's own build number, not the wire protocol",
    "ET_PROTO_VERSION": "checked by name in PAIRS",
}


@pytest.mark.parametrize("c_name,py_name", PAIRS)
def test_constant_matches_firmware(c_name, py_name):
    d = _defines()
    assert c_name in d, "%s is not defined in %s" % (c_name, HEADER)
    assert getattr(P, py_name) == d[c_name], (
        "%s is %d in the firmware and %s is %d on the host"
        % (c_name, d[c_name], py_name, getattr(P, py_name)))


def test_every_protocol_constant_is_mirrored():
    """PAIRS must not go stale when the protocol grows.

    WHAT THIS DOES NOT COVER. It checks that a host name EXISTS for every
    command, response, error, transport and link speed in the header, and PAIRS
    checks the values. It says nothing about whether either side implements the
    command, nor about the LAYOUT of any payload -- the v2 INFO and CONFIG tails
    are parsed by offsets that only a live board can falsify. Those belong to
    `device.py` and to the acceptance capture, not here.

    It exists because the first version of this file was a hand-written table,
    and a hand-written table is exactly the thing that is green on the day a
    constant is added to one side only.
    """
    d = _defines()
    missing = []
    for c_name in sorted(d):
        if c_name in NOT_MIRRORED:
            continue
        if not c_name.startswith(MIRRORED_PREFIXES):
            continue
        py_name = c_name[3:]                     # drop "ET_"
        if not hasattr(P, py_name):
            missing.append(c_name)
    assert not missing, (
        "the firmware header defines %s with no mirror in proto.py. Add the "
        "constant and a PAIRS row, or list it in NOT_MIRRORED with a reason."
        % ", ".join(missing))


def test_pairs_covers_every_mirrored_constant():
    """Every mirrored constant has a PAIRS row, so its VALUE is checked too."""
    d = _defines()
    in_pairs = {c for c, _ in PAIRS}
    missing = [c for c in sorted(d)
               if c.startswith(MIRRORED_PREFIXES)
               and c not in NOT_MIRRORED and c not in in_pairs]
    assert not missing, (
        "these are mirrored but their values are never compared: %s"
        % ", ".join(missing))


def test_bank_entry_fits_a_frame():
    """A single maximum-size request must fit in one SET_BANK frame.

    Otherwise `set_request_bank` has an entry it can never send, and it would
    find out one request into a 2 000-entry upload.
    """
    assert P.MAX_FRAME_IN >= P.MAX_REQ + 3


def test_crc_fixed_vector():
    """CCITT-FALSE over b'123456789' is 0x29B1 -- the standard check value.

    Pinned so the host implementation cannot drift into a different CRC-16
    variant (there are several, and they differ only on real data).
    """
    assert P.crc16(b"123456789") == 0x29B1


def test_frame_round_trip():
    payload = bytes(range(64))
    frame = P.encode(P.CMD_SET_REQUEST, payload)
    dec = P.Decoder()
    got = dec.feed(frame)
    assert got == [(P.CMD_SET_REQUEST, payload)]


def test_frame_round_trip_byte_at_a_time():
    """The decoder is fed one byte at a time, as a slow serial link delivers."""
    frame = P.encode(P.RSP_BATCH_DATA, b"\x00\x01\x02\x03")
    dec = P.Decoder()
    out = []
    for i in range(len(frame)):
        out.extend(dec.feed(frame[i:i + 1]))
    assert out == [(P.RSP_BATCH_DATA, b"\x00\x01\x02\x03")]


def test_corrupt_crc_raises():
    frame = bytearray(P.encode(P.CMD_PING, b"\x01\x02"))
    frame[-1] ^= 0xFF
    with pytest.raises(P.FrameError):
        P.Decoder().feed(bytes(frame))


def test_leading_garbage_is_skipped():
    """A resync must not need a reset: the device may be mid-stream."""
    frame = P.encode(P.CMD_PING)
    dec = P.Decoder()
    assert dec.feed(b"\x00\xff" + b"E" + frame) == [(P.CMD_PING, b"")]


def test_two_frames_in_one_read():
    a = P.encode(P.CMD_PING)
    b = P.encode(P.RSP_ACK, b"\x01\x00")
    assert P.Decoder().feed(a + b) == [(P.CMD_PING, b""),
                                       (P.RSP_ACK, b"\x01\x00")]
