# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""Hold the password demo's constants and estimators against the firmware.

No hardware. Two things are checked here and they fail in different ways:

* **The constants.** `demos/password/pw.py` mirrors offsets out of
  `firmware/victim/inc/et_victim.h`. A guess offset that drifts does not raise:
  the device compares the wrong bytes, every candidate looks the same, and the
  scan reports a flat result -- which is indistinguishable from a constant-time
  comparison. That is the worst possible failure for this demo, because it is the
  demo's own success condition.

* **The estimator.** `pw.trimmed_mean` exists because a plain mean and a plain
  median each failed on real data, in opposite ways. Those two failures are
  reproduced here from synthetic records so that a future simplification back to
  either one fails a test instead of a capture.
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
for _p in (os.path.join(_ROOT, "demos", "password"),
           os.path.join(_ROOT, "demos", "jitter")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import etv                                                     # noqa: E402
import pw                                                      # noqa: E402

HEADER = os.path.join(_ROOT, "firmware", "victim", "inc", "et_victim.h")
PWCHECK_H = os.path.join(_ROOT, "firmware", "common", "pwcheck.h")
PWCHECK_C = os.path.join(_ROOT, "firmware", "common", "pwcheck.c")


def _defines(path, prefix="ETV_"):
    out = {}
    pat = re.compile(r"^#define\s+(%s[A-Z0-9_]+)\s+(0x[0-9A-Fa-f]+|\d+)u?\b"
                     % prefix)
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            m = pat.match(line.strip())
            if m:
                out[m.group(1)] = int(m.group(2), 0)
    return out


PAIRS = [
    ("ETV_CMD_PWCHECK", "CMD_PWCHECK"),
    ("ETV_CMD_PWCHECK_CT", "CMD_PWCHECK_CT"),
    ("ETV_PW_OFF", "PW_OFF"),
    ("ETV_PW_GUESS_OFF", "PW_GUESS_OFF"),
    ("ETV_PW_MAX_GUESS", "PW_MAX_GUESS"),
    ("ETV_F_PW_MATCH", "F_PW_MATCH"),
    ("ETV_F_PW_BAD_REQ", "F_PW_BAD_REQ"),
]


@pytest.mark.parametrize("c_name,py_name", PAIRS)
def test_constant_matches_firmware(c_name, py_name):
    d = _defines(HEADER)
    assert c_name in d, "%s is not defined in %s" % (c_name, HEADER)
    assert getattr(pw, py_name) == d[c_name], (
        "%s is %d in the firmware and pw.%s is %d"
        % (c_name, d[c_name], py_name, getattr(pw, py_name)))


def test_the_commands_do_not_collide_with_the_existing_ones():
    """A new command that reuses a value silently becomes the old one."""
    vals = [etv.CMD_MEASURE, etv.CMD_STATUS, pw.CMD_PWCHECK, pw.CMD_PWCHECK_CT]
    assert len(set(vals)) == len(vals), "two commands share a value: %r" % vals


def test_the_flags_do_not_collide_either():
    vals = [etv.F_LINK_100F, etv.F_CYC_WRAP, pw.F_PW_MATCH, pw.F_PW_BAD_REQ]
    assert len(set(vals)) == len(vals)
    for v in vals:
        assert v and (v & (v - 1)) == 0, "%#x is not a single bit" % v


def test_guess_offset_leaves_room_for_the_length_byte():
    """The firmware reads guess_len at PW_OFF and the guess at PW_GUESS_OFF."""
    assert pw.PW_OFF >= etv.REQ_HDR, (
        "the guess length would overlap the request header")
    assert pw.PW_GUESS_OFF >= pw.PW_OFF + 2, (
        "no room for guess_len and its reserved byte")


def test_request_puts_the_guess_where_the_firmware_reads_it():
    guess = b"abcdefgh"
    req = pw.request(guess, tag=0x01020304, pad_to=64)
    assert struct.unpack_from("<I", req, 0)[0] == etv.MAGIC_REQ
    assert req[4] == etv.VERSION
    assert req[5] == pw.CMD_PWCHECK
    assert struct.unpack_from("<I", req, 8)[0] == 0x01020304
    assert req[pw.PW_OFF] == len(guess)
    assert req[pw.PW_OFF + 1] == 0
    assert req[pw.PW_GUESS_OFF:pw.PW_GUESS_OFF + len(guess)] == guess
    assert len(req) == 64


def test_const_time_request_differs_only_in_the_command():
    a = pw.request(b"abcdefgh", tag=7, pad_to=64)
    b = pw.request(b"abcdefgh", tag=7, pad_to=64, const_time=True)
    assert a[5] == pw.CMD_PWCHECK and b[5] == pw.CMD_PWCHECK_CT
    assert a[:5] == b[:5] and a[6:] == b[6:], (
        "the control request must differ from the measured one in the command "
        "byte and nothing else, or the comparison compares two things")


def test_request_refuses_an_over_long_guess():
    with pytest.raises(ValueError, match="at most"):
        pw.request(b"x" * (pw.PW_MAX_GUESS + 1))


def test_the_firmware_has_both_comparisons_and_one_of_them_returns_early():
    """A crude read of the source, for one specific regression.

    If `pw_check_early` ever stops returning early it still compiles, still
    answers correctly, and the demo quietly becomes its own control -- a flat
    result that reads as "constant time". Cheap to notice here.
    """
    src = open(PWCHECK_C, "r", encoding="utf-8").read()
    assert "pw_check_early" in src and "pw_check_const" in src
    early = src[src.index("pw_check_early"):src.index("pw_check_const")]
    assert "break" in early or "return 0" in early, (
        "pw_check_early no longer leaves its loop early; the demo would have "
        "nothing to recover and would look like a success for the mitigation")
    const = src[src.index("int pw_check_const"):]
    body = const[:const.index("\n}")]
    assert "break" not in body, (
        "pw_check_const must not leave its loop early -- that is the whole "
        "property it exists to have")


def test_rounds_amplifier_defaults_to_one():
    """The honest default. A committed tree with the amplifier on would publish
    a step eight times the real one."""
    d = _defines(PWCHECK_H, prefix="ETV_PW_")
    assert d.get("ETV_PW_ROUNDS") == 1, (
        "ETV_PW_ROUNDS is %r; the default build must measure one pass"
        % d.get("ETV_PW_ROUNDS"))


# ---------------------------------------------------------------- estimators --

def _records(cyc, hz=180000000, bad=None):
    """Synthetic decoded records with the given per-exchange cycle counts."""
    cyc = np.asarray(cyc, np.int64)
    n = len(cyc)
    return dict(
        victim_cyc=cyc,
        victim_us=cyc.astype(np.float64) / hz * 1e6,
        clk_hz=np.full(n, hz, np.uint32),
        pw_bad_req=(np.zeros(n, bool) if bad is None else np.asarray(bad, bool)),
        pw_match=np.zeros(n, bool),
        flags=np.zeros(n, np.uint8),
    )


def test_a_plain_mean_is_beaten_by_the_excursion_tail():
    """The failure that picked a wrong byte on real hardware.

    The measured ingredients: a wrong byte reads 519 cycles, a right one 530 --
    a step of 11 -- and a variable share of exchanges come in about 90 cycles
    high from something that is not the comparison. Measured excursion rates on
    this bench ranged from 0.4 % to 11.5 % of a group depending on the build.

    An excursion rate of 14 % on the wrong candidate is enough to invert the
    means: (172*519 + 28*609)/200 = 531.6 against the right byte's clean 530. So
    the arithmetic needs no luck, and the real scan duly picked a wrong byte
    whose MEDIAN sat exactly on the floor.
    """
    wrong = _records([519] * 172 + [609] * 28)
    right = _records([530] * 200)

    assert pw.mean_us(wrong) > pw.mean_us(right), (
        "this test encodes the failure; if the means no longer invert, update "
        "the numbers rather than deleting it")
    # And the estimator actually used gets it right.
    assert pw.median_cyc(wrong) < pw.median_cyc(right)
    thr = pw.EXCURSION_CYC / 180e6 * 1e6
    assert (pw.trimmed_mean(wrong["victim_us"], thr)
            < pw.trimmed_mean(right["victim_us"], thr))


def test_a_plain_median_ties_on_quantised_data():
    """The other failure: the round trip is quantised, so medians collide.

    Two candidates whose true means differ by half a quantum land on the same
    grid point in a median, and the scan reports `+0.0000 us clear`.
    """
    q = 0.08                                   # one byte-time, microseconds
    # 60 % of a's records on the high grid point, 90 % of b's. Both medians are
    # therefore the high point and they TIE, while the means differ by a third of
    # a quantum -- which is the shape of the real round-trip data.
    a = np.array([17.0 + q] * 60 + [17.0] * 40)
    b = np.array([17.0 + q] * 90 + [17.0] * 10)

    assert np.median(a) == np.median(b), "this test needs the medians to tie"
    thr = pw.EXCURSION_CYC / 180e6 * 1e6
    assert pw.trimmed_mean(b, thr) > pw.trimmed_mean(a, thr), (
        "the trimmed mean must separate what the median ties")


def test_trimmed_mean_drops_the_tail_and_keeps_the_rest():
    x = np.array([1.0] * 99 + [100.0])
    assert pw.trimmed_mean(x, 0.5) == pytest.approx(1.0)
    # Everything above the median: fall back to the median rather than nan.
    assert np.isfinite(pw.trimmed_mean(np.array([1.0, 2.0]), -5.0))


def test_unparseable_records_are_excluded():
    """A request the device could not parse ran no check, so its timing is the
    baseline. Averaging it in pulls every candidate towards the same number --
    the failure mode where a sweep comes out flat and reads as constant time."""
    d = _records([530] * 10 + [100] * 10,
                 bad=[False] * 10 + [True] * 10)
    assert pw.median_cyc(d) == 530
    assert int(pw.usable(d).sum()) == 10


def test_modal_fraction_reports_concentration():
    assert pw.modal_fraction(_records([519] * 99 + [609])) == pytest.approx(0.99)
    assert pw.modal_fraction(_records(list(range(100)))) == pytest.approx(0.01)
