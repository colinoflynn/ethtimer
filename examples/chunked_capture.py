# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""Run a capture as CHUNKS, so a partial answer exists while it is still running.

`campaign.run` accumulates in host memory and writes one `.npz` at the end. For
a capture measured in minutes that is fine; for one measured in hours it means
no reading until it finishes, and a crash at 90 % costs the lot. This runs the
same campaign as a sequence of smaller ones and writes a file per chunk:

    results/<name>_000.npz, _001.npz, ...

Any analysis that can pool several captures can then be run **at any moment**
against whatever has landed. Nothing is merged, deliberately: merging means
re-basing each chunk's `index`, and an off-by-one there pairs every record with
a neighbouring class without failing loudly.

    python examples/chunked_capture.py --target snmpv3 \
        --port COM10 --victim 192.168.7.10 --ip 192.168.7.20 \
        --n 60000 --chunk 5000 --out runs/snmpv3

WHAT A CHUNK COSTS. Each one re-runs the target's `prepare()`, so a target that
opens a session gets a **fresh session per chunk**. That is usually what you
want -- it bounds the damage of a session dying, and it re-randomises anything
seeded in `prepare()` -- but it is not free, and for a target whose setup is
expensive the chunk should not be small. A target whose `prepare()` runs a key
agreement on the device pays that cost per chunk.

WHAT THIS DOES NOT DO. It does not make the chunks statistically independent
of each other: they share the device, the link and the hour. Pool them for
precision, not for a replication claim -- for that, compare chunks against each
other, which is what a time-ordered split test does.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from ethtimer import Device, campaign, target as T, proto as P   # noqa: E402
from ethtimer.cli import _BUILTIN, _load_module                  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--module", help="an adapter .py; omit to use --target's")
    p.add_argument("--target", required=True)
    p.add_argument("--port", required=True)
    p.add_argument("--ip"); p.add_argument("--mask", default="255.255.255.0")
    p.add_argument("--gw"); p.add_argument("--victim", required=True)
    p.add_argument("--n", type=int, required=True)
    p.add_argument("--chunk", type=int, default=5000)
    p.add_argument("--gap", type=int, default=0)
    p.add_argument("--out", required=True, help="prefix; _NNN.npz is appended")
    a = p.parse_args(argv)

    path = a.module or _BUILTIN.get(a.target)
    if path is None:
        p.error("unknown target %r and no --module; "
                "`python -m ethtimer.cli --list` shows the shipped ones"
                % a.target)
    _load_module(path)

    done, chunk_i, t0 = 0, 0, time.time()
    while done < a.n:
        want = min(a.chunk, a.n - done)
        out = "%s_%03d.npz" % (a.out, chunk_i)
        with Device(a.port) as d:
            if a.ip:
                d.set_net(ip=a.ip, mask=a.mask, gw=a.gw or a.ip, victim=a.victim)
            tgt = T.get(a.target)()
            d.set_target(port=tgt.port, timeout_ms=tgt.timeout_ms,
                         protocol=tgt.transport)
            if tgt.transport == P.PROTO_TCP:
                d.tcp_connect()
            # `campaign.run`'s `out=` is only the crash-partial path; the save
            # is `Result.save`, which is what the CLI does. Calling run() alone
            # reports success and writes nothing -- it did exactly that here
            # once, which is why this line is two lines.
            res = campaign.run(d, tgt, n=want, gap_us=a.gap, out=out,
                               progress=False)
            written = res.save(os.path.abspath(out))
            # GIVE THE VICTIM ITS STATE BACK. `Device.close()` closes the serial
            # port and nothing else, so without these two lines every chunk
            # leaks the board's TCP connection and whatever session the target
            # opened on it. A dozen chunks get away with it; a few hundred do
            # not, and the failure is a chunk that WEDGES -- the victim stops
            # answering, no error is raised, and the capture sits there. Both
            # calls swallow their own errors: cleanup must not fail a chunk
            # whose data is already on disk.
            if hasattr(tgt, "close_session"):
                try:
                    tgt.close_session(d)
                except Exception:
                    pass
            if tgt.transport == P.PROTO_TCP:
                d.tcp_close()
        done += want
        chunk_i += 1
        rate = done / max(1e-9, time.time() - t0)
        print("chunk %d: %d/%d done, %.0f/s, eta %.1f min -> %s"
              % (chunk_i - 1, done, a.n, rate, (a.n - done) / rate / 60,
                 written), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
