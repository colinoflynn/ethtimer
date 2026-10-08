# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""Decompose a `jitter` capture into the path and the two endpoints.

    python demos/jitter/analyse.py run.npz
    python demos/jitter/analyse.py baseline.npz --vs through_switch.npz

A capture holds two independent clocks' worth of information:

    round trip   dt_hw, in the INSTRUMENT's cycles: its transmit-complete
                 interrupt to its receive interrupt
    responder    tx_cyc - rx_cyc, in the RESPONDER's cycles: its receive
                 interrupt to the moment it handed the reply on
    path         the difference

WHY THE SUBTRACTION IS THE WHOLE POINT. Measured on a direct cable between two
NUCLEO-F429ZI boards, 20 000 exchanges of 64-byte frames:

    round trip   median 190.494 us   sd 61.300 us
    responder    median 163.567 us   sd 62.144 us
    path         median  21.328 us   sd  2.816 us

The responder is slow and it jitters, and **it does not matter**, because it
says so. Taking its own interval off leaves a path figure with a twentieth of
the variation. A responder built to be fast and then assumed constant would
have put all 61 us of that into somebody's conclusion about a switch.

READ THE VARIANCE, NOT THE MEAN. `path` still contains two frame
serialisations, two MAC transmit paths and the instrument's own interrupt
latency. Those are constant for a fixed frame size and they are most of the
21 us above -- on a direct cable there is nothing else it could be. So:

* `sd`, `p99 - p50` and the tail are the path's, and they are what this is for;
* the median is a latency plus a constant, and the only honest use of it is
  `--vs`, which differences two runs that share a frame size.

`--vs` prints that difference and nothing else, because that is the only form
in which "the switch adds N microseconds" is a sentence this tool can support.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import etv                                                     # noqa: E402

#: The percentiles worth printing. A jitter measurement lives in the tail, so
#: the top end is sampled finely and the middle is not: a distribution whose
#: p50 and p99.9 differ by 6 us and one where they differ by 600 us are
#: different networks, and a mean and a standard deviation report them
#: similarly.
PCTS = (0.1, 1, 25, 50, 75, 90, 99, 99.9)


def load(path: str) -> dict:
    """Read a capture and recover the three series, in microseconds."""
    d = np.load(path, allow_pickle=False)
    need = ("dt_hw", "clock_hz", "victim_us")
    missing = [k for k in need if k not in d.files]
    if missing:
        raise SystemExit(
            "%s is missing %s. Is it a `jitter` capture? `--target jitter` "
            "writes the responder's own interval; no other target has one to "
            "write." % (path, ", ".join(missing)))

    hz = float(d["clock_hz"])
    if hz <= 0:
        raise SystemExit("%s records an instrument clock of %r" % (path, hz))

    rtt = d["dt_hw"].astype(np.float64) / hz * 1e6
    vic = d["victim_us"].astype(np.float64)
    out = dict(
        path_file=path,
        rtt=rtt,
        victim=vic,
        net=rtt - vic,
        frame=int(d["frame_bytes"]) if "frame_bytes" in d.files else 0,
        gap=int(d["gap_us"]) if "gap_us" in d.files else 0,
        instrument_hz=hz,
        responder_hz=int(d["victim_hz"][0]) if "victim_hz" in d.files else 0,
        board=str(d["board"]) if "board" in d.files else "?",
        n_timeout=int(d["n_timeout"]) if "n_timeout" in d.files else 0,
        n_short=int(d["n_short"]) if "n_short" in d.files else 0,
        n_bad_dt=int(d["n_bad_dt"]) if "n_bad_dt" in d.files else 0,
        n_requested=int(d["n_requested"]) if "n_requested" in d.files else len(rtt),
    )
    if "victim_seq" in d.files:
        out["cont"] = etv.continuity({"seq": d["victim_seq"]})
    if "victim_flags" in d.files:
        f = d["victim_flags"]
        out["not_100f"] = int((~(f & etv.F_LINK_100F).astype(bool)).sum())
        out["wrapped"] = int((f & etv.F_CYC_WRAP).astype(bool).sum())
    return out


def table(rows) -> None:
    """One row per series, percentiles across."""
    head = "%-12s %8s" % ("", "n")
    for p in PCTS:
        head += " %9s" % ("p%g" % p)
    head += " %9s %9s" % ("max", "sd")
    print(head)
    print("-" * len(head))
    for name, a in rows:
        line = "%-12s %8d" % (name, len(a))
        for p in PCTS:
            line += " %9.3f" % np.percentile(a, p)
        line += " %9.3f %9.3f" % (a.max(), a.std())
        print(line)


def report(c: dict) -> None:
    print("%s" % c["path_file"])
    print("  %s, %d-byte frames each way, %d us requested gap"
          % (c["board"], c["frame"], c["gap"]))
    print("  instrument %.0f Hz, responder %d Hz"
          % (c["instrument_hz"], c["responder_hz"]))
    print("  %d records of %d requested; %d timeouts, %d short, %d bad dt"
          % (len(c["rtt"]), c["n_requested"], c["n_timeout"], c["n_short"],
             c["n_bad_dt"]))

    cont = c.get("cont")
    if cont:
        if cont["backwards"]:
            print("  *** %d records OUT OF ORDER (the responder's own counter "
                  "went backwards). Nothing but that counter can see this: "
                  "from the instrument's side a reordered reply is just the "
                  "next datagram. ***" % cont["backwards"])
        if cont["gaps"]:
            print("  %d gaps in the responder's counter, %d replies "
                  "unaccounted for" % (cont["gaps"], cont["missing"]))
        if not cont["gaps"] and not cont["backwards"]:
            print("  responder counter contiguous over %d replies: nothing "
                  "lost, nothing reordered"
                  % (cont["last"] - cont["first"] + 1))
    if c.get("not_100f"):
        print("  *** %d records taken while the responder's link was not "
              "100 Mbit full duplex; serialisation is the largest constant in "
              "the path figure and it changed ***" % c["not_100f"])
    if c.get("wrapped"):
        print("  %d records span a responder cycle-counter wrap; the "
              "subtraction handles it" % c["wrapped"])

    print()
    table([("round trip", c["rtt"]), ("responder", c["victim"]),
           ("path", c["net"])])
    print()

    net = c["net"]
    print("  PATH: median %.3f us, and the number to quote is the SPREAD:"
          % np.median(net))
    print("        p99 - p50 = %+.3f us,  max - p50 = %+.3f us,  sd = %.3f us"
          % (np.percentile(net, 99) - np.median(net),
             net.max() - np.median(net), net.std()))
    print("        The median has two frame serialisations and two MAC "
          "transmit paths in it.")
    print("        Use --vs against a direct-cable baseline to get a device's "
          "own contribution.")

    if np.median(c["victim"]) > 0.5 * np.median(c["rtt"]):
        print()
        print("  NOTE: the responder accounts for %.0f%% of the round trip and "
              "%.0f%% of its variance."
              % (100.0 * np.median(c["victim"]) / np.median(c["rtt"]),
                 100.0 * c["victim"].std() / max(1e-9, c["rtt"].std())))
        print("        That is expected and it is why it reports its own "
              "interval; the path column is the one to read.")
    if np.any(c["victim"] <= 0):
        print()
        print("  *** %d records report a responder interval of zero or less. "
              "Its cycle counter was not running, and the path column is then "
              "the round trip with nothing taken off. ***"
              % int((c["victim"] <= 0).sum()))


def compare(a: dict, b: dict) -> None:
    """b minus a, which is the only form in which a device's latency is a fact
    this tool can state."""
    print()
    print("=" * 78)
    print("DIFFERENCE: %s  minus  %s" % (b["path_file"], a["path_file"]))
    if a["frame"] != b["frame"]:
        print("  *** the two runs used %d- and %d-byte frames. Serialisation "
              "is 80 ns per byte each way at 100 Mbit, so this difference is "
              "partly frame size and the comparison is not clean. ***"
              % (a["frame"], b["frame"]))
    print()
    print("  %-12s %12s %12s %12s" % ("", "baseline", "under test", "delta"))
    for label, f in (("median", np.median),
                     ("p99", lambda x: np.percentile(x, 99)),
                     ("p99.9", lambda x: np.percentile(x, 99.9)),
                     ("max", np.max),
                     ("sd", np.std)):
        pa, pb = f(a["net"]), f(b["net"])
        print("  path %-7s %12.3f %12.3f %+12.3f" % (label, pa, pb, pb - pa))
    print()
    print("  The delta in the median is the device under test's one-way-plus-"
          "return latency;")
    print("  the delta in the spread is the jitter it adds. Both are in "
          "microseconds.")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture", help="a .npz from --target jitter")
    ap.add_argument("--vs", metavar="CAPTURE",
                    help="a second capture; print it minus the first. Use the "
                         "direct-cable run as the first one")
    ap.add_argument("--hist", type=int, default=0, metavar="BINS",
                    help="also print a text histogram of the path series")
    a = ap.parse_args(argv)

    base = load(a.capture)
    report(base)

    if a.hist:
        print()
        hist(base["net"], a.hist)

    if a.vs:
        other = load(a.vs)
        print()
        report(other)
        compare(base, other)
    return 0


def hist(x: np.ndarray, bins: int) -> None:
    """A text histogram, clipped at p99.9 so one outlier cannot flatten it."""
    hi = np.percentile(x, 99.9)
    lo = x.min()
    if hi <= lo:
        print("  (histogram: every value is %.3f us)" % lo)
        return
    counts, edges = np.histogram(np.clip(x, lo, hi), bins=bins,
                                 range=(lo, hi))
    wide = counts.max()
    print("  path, %d bins from %.3f to %.3f us (clipped at p99.9; %d above)"
          % (bins, lo, hi, int((x > hi).sum())))
    for k, c in enumerate(counts):
        bar = "#" * int(round(60.0 * c / wide)) if wide else ""
        print("    %9.3f %7d %s" % (edges[k], c, bar))


if __name__ == "__main__":
    raise SystemExit(main())
