# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""Hold the reference responder's firmware header and the host's mirror together.

No hardware and no toolchain: this parses `firmware/victim/inc/et_victim.h` and
checks `demos/jitter/etv.py` against it, the same way `test_proto.py` does for
the instrument's own protocol.

It matters more here than the line count suggests. The responder's reply carries
two cycle counters at fixed offsets, and the host subtracts one from the other
to produce the number the whole demo exists for. An offset that drifts by four
bytes does not fail: it swaps `tx_cyc` for `clk_hz`, and the path figure comes
out as a large negative number or as a plausible small one, depending on which
way it went.

WHAT THIS DOES NOT COVER. It checks the constants and the reply layout. It says
nothing about whether the firmware fills those fields with anything sensible --
that needs a board, and the field that went wrong in practice (`rx_cyc` reading
a frozen value because the cycle counter had stopped) was correct in layout and
useless in content. `demos/jitter/jitter_target.py` refuses a capture whose
probe reports a zero interval, which is where that one is caught.
"""
from __future__ import annotations

import os
import re
import struct
import sys

import numpy as np
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_DEMO = os.path.join(_ROOT, "demos", "jitter")
if _DEMO not in sys.path:
    sys.path.insert(0, _DEMO)

import etv                                                     # noqa: E402

HEADER = os.path.join(_ROOT, "firmware", "victim", "inc", "et_victim.h")


def _defines():
    """Every simple `#define ETV_NAME <int>` in the firmware header."""
    out = {}
    pat = re.compile(r"^#define\s+(ETV_[A-Z0-9_]+)\s+(0x[0-9A-Fa-f]+|\d+)u?\b")
    with open(HEADER, "r", encoding="utf-8") as fh:
        for line in fh:
            m = pat.match(line.strip())
            if m:
                out[m.group(1)] = int(m.group(2), 0)
    return out


#: firmware name -> host name. Every ETV_ constant must be here or in
#: NOT_MIRRORED, and `test_every_constant_is_mirrored` enforces that.
PAIRS = [
    ("ETV_VERSION", "VERSION"),
    ("ETV_PORT", "PORT"),
    ("ETV_MAGIC_REQ", "MAGIC_REQ"),
    ("ETV_MAGIC_RSP", "MAGIC_RSP"),
    ("ETV_REQ_HDR", "REQ_HDR"),
    ("ETV_RSP_HDR", "RSP_HDR"),
    ("ETV_CMD_MEASURE", "CMD_MEASURE"),
    ("ETV_CMD_STATUS", "CMD_STATUS"),
    ("ETV_F_LINK_100F", "F_LINK_100F"),
    ("ETV_F_CYC_WRAP", "F_CYC_WRAP"),
    ("ETV_MAX_REPLY", "MAX_REPLY"),
]

NOT_MIRRORED = {
    # The four IP octets are a BUILD-TIME default for the responder's own
    # address and deliberately not protocol: the host never sends them and
    # never needs to agree about them. They are overridden with EXTRA_DEFS.
    "ETV_IP0": "build-time default address, not wire protocol",
    "ETV_IP1": "build-time default address, not wire protocol",
    "ETV_IP2": "build-time default address, not wire protocol",
    "ETV_IP3": "build-time default address, not wire protocol",
    # The password demo's half of the protocol is mirrored in
    # demos/password/pw.py, not in demos/jitter/etv.py, because the instrument's
    # own library has no business knowing about it -- and it is checked just as
    # exhaustively, in tests/test_password_proto.py. Listed here by name rather
    # than by prefix so that adding a third command cannot slip through both
    # files at once.
    "ETV_CMD_PWCHECK": "mirrored in demos/password/pw.py",
    "ETV_CMD_PWCHECK_CT": "mirrored in demos/password/pw.py",
    "ETV_PW_OFF": "mirrored in demos/password/pw.py",
    "ETV_PW_GUESS_OFF": "mirrored in demos/password/pw.py",
    "ETV_PW_MAX_GUESS": "mirrored in demos/password/pw.py",
    "ETV_F_PW_MATCH": "mirrored in demos/password/pw.py",
    "ETV_F_PW_BAD_REQ": "mirrored in demos/password/pw.py",
}


@pytest.mark.parametrize("c_name,py_name", PAIRS)
def test_constant_matches_firmware(c_name, py_name):
    d = _defines()
    assert c_name in d, "%s is not defined in %s" % (c_name, HEADER)
    assert getattr(etv, py_name) == d[c_name], (
        "%s is %d (%#x) in the firmware and %s is %d (%#x) on the host"
        % (c_name, d[c_name], d[c_name], py_name,
           getattr(etv, py_name), getattr(etv, py_name)))


def test_every_constant_is_mirrored():
    """PAIRS must not go stale when a field is added.

    A hand-written table is exactly the thing that stays green on the day a
    constant is added to one side only; this is the check that makes the table
    honest, and it is why NOT_MIRRORED carries a reason per entry rather than
    just a name.
    """
    d = _defines()
    mirrored = {c for c, _ in PAIRS}
    missing = [c for c in sorted(d)
               if c not in mirrored and c not in NOT_MIRRORED]
    assert not missing, (
        "%s defines %s with no entry in PAIRS and no reason in NOT_MIRRORED"
        % (os.path.basename(HEADER), ", ".join(missing)))


def test_magic_values_spell_what_the_header_says():
    """The magics are u32s in the source and four ASCII bytes on the wire."""
    assert struct.pack("<I", etv.MAGIC_REQ) == b"ETRQ"
    assert struct.pack("<I", etv.MAGIC_RSP) == b"ETVR"


def test_reply_field_table_tiles_the_header_exactly():
    """RSP_FIELDS must cover the reply header with no overlap and no gap.

    Not a style check. The decoder reads each field at the offset in this table,
    and two fields claiming one offset -- or a field running past the end of the
    kept window -- is how a cycle counter gets read as a clock frequency.
    """
    seen = {}
    for name, (off, fmt) in etv.RSP_FIELDS.items():
        size = struct.calcsize(fmt)
        assert off + size <= etv.RSP_HDR, (
            "%s is at offset %d size %d, past the %d-byte reply header"
            % (name, off, size, etv.RSP_HDR))
        for b in range(off, off + size):
            assert b not in seen, (
                "byte %d is claimed by both %s and %s" % (b, seen[b], name))
            seen[b] = name
    # The two reserved gaps (byte 6..7, and the padding after 32) are allowed to
    # be unclaimed; everything up to n_seen must not be.
    for b in list(range(0, 6)) + list(range(8, etv.RSP_HDR)):
        assert b in seen, "byte %d of the reply header is in no field" % b


def test_request_is_the_header_length_and_pads_to_order():
    assert len(etv.request()) == etv.REQ_HDR
    assert len(etv.request(pad_to=64)) == 64
    assert len(etv.request(pad_to=etv.MAX_REPLY)) == etv.MAX_REPLY
    # Shorter than the header is not padding down, it is left alone.
    assert len(etv.request(pad_to=4)) == etv.REQ_HDR
    with pytest.raises(ValueError):
        etv.request(pad_to=etv.MAX_REPLY + 1)


def test_request_fields_land_where_the_firmware_reads_them():
    req = etv.request(tag=0x11223344, cmd=etv.CMD_STATUS, pad_to=40)
    assert struct.unpack_from("<I", req, 0)[0] == etv.MAGIC_REQ
    assert req[4] == etv.VERSION
    assert req[5] == etv.CMD_STATUS
    assert struct.unpack_from("<H", req, 6)[0] == 0        # reserved, must be 0
    assert struct.unpack_from("<I", req, 8)[0] == 0x11223344
    assert req[etv.REQ_HDR:] == bytes(40 - etv.REQ_HDR)    # padding is zeroes


def _reply(**kw):
    """Pack a reply from named fields, through RSP_FIELDS."""
    b = bytearray(etv.RSP_HDR)
    vals = dict(magic=etv.MAGIC_RSP, ver=etv.VERSION, flags=0, tag=0, seq=0,
                rx_cyc=0, tx_cyc=0, clk_hz=180000000, n_seen=0)
    vals.update(kw)
    for name, (off, fmt) in etv.RSP_FIELDS.items():
        struct.pack_into(fmt, b, off, vals[name])
    return np.frombuffer(bytes(b), np.uint8).reshape(1, etv.RSP_HDR)


def test_decode_round_trips_every_field():
    w = _reply(tag=0xDEADBEEF, seq=12345, rx_cyc=1000, tx_cyc=1900,
               clk_hz=180000000, n_seen=99, flags=etv.F_LINK_100F)
    d = etv.decode(w)
    etv.check(d)
    assert int(d["tag"][0]) == 0xDEADBEEF
    assert int(d["seq"][0]) == 12345
    assert int(d["n_seen"][0]) == 99
    assert int(d["victim_cyc"][0]) == 900
    assert abs(float(d["victim_us"][0]) - 900 / 180e6 * 1e6) < 1e-9


def test_interval_is_correct_across_a_counter_wrap():
    """The counter is 32 bits and wraps every 23.9 s at 180 MHz.

    A capture is minutes long, so a wrap inside an exchange is routine rather
    than exotic, and the subtraction has to be modular. Taken literally the
    difference would be about 23 seconds, which is not a reading anyone would
    attribute to a cable -- but it would move a mean and destroy a standard
    deviation, which is what this demo reports.
    """
    w = _reply(rx_cyc=0xFFFFFF00, tx_cyc=0x00000100)
    d = etv.decode(w)
    assert int(d["victim_cyc"][0]) == 0x200


def test_check_rejects_a_drifted_window():
    w = _reply()
    w = np.asarray(w).copy()
    w[0, 0] ^= 1
    with pytest.raises(RuntimeError, match="magic"):
        etv.check(etv.decode(w))


def test_check_rejects_a_version_mismatch():
    w = np.asarray(_reply()).copy()
    w[0, 4] = etv.VERSION + 1
    with pytest.raises(RuntimeError, match="v%d" % (etv.VERSION + 1)):
        etv.check(etv.decode(w))


def test_check_rejects_a_zero_clock():
    """A zero clock means the interval cannot be converted at all, so the path
    figure would be the round trip with nothing taken off."""
    w = np.asarray(_reply(clk_hz=0)).copy()
    with pytest.raises(RuntimeError, match="0 Hz"):
        etv.check(etv.decode(w))


def test_continuity_sees_loss_and_reordering():
    """What the instrument's timeout count cannot see."""
    def seqs(xs):
        return etv.continuity({"seq": np.array(xs, np.uint32)})

    assert seqs([1, 2, 3, 4])["gaps"] == 0
    assert seqs([1, 2, 3, 4])["backwards"] == 0

    lost = seqs([1, 2, 5, 6])
    assert lost["gaps"] == 1 and lost["missing"] == 2

    swapped = seqs([1, 3, 2, 4])
    assert swapped["backwards"] == 1, (
        "a reordered reply must be visible; from the instrument's side it is "
        "just the next datagram")


def test_decode_refuses_a_window_too_short_to_hold_the_header():
    with pytest.raises(ValueError, match="reply header"):
        etv.decode(np.zeros((4, etv.RSP_HDR - 1), np.uint8))
