# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""One command line for every protocol.

    python -m ethtimer.cli --list
    python -m ethtimer.cli --ports
    python -m ethtimer.cli --target snmpv3 --n 20000 --out run.npz
    python -m ethtimer.cli --target oscore --n 20000
    python -m ethtimer.cli --module path/to/my_target.py --n 5000

`--target` names an adapter from `demos/`; `--module` loads one from a file,
which is what a new protocol needs before it is worth adding to the table below.
Either way the capture, the diagnostics, the acceptance check and the saved
layout are `ethtimer.campaign`'s and are the same for all of them.

NETWORK DEFAULTS. The instrument's own address is runtime state on the device,
so the same binary drives a victim on any subnet; `--ip/--mask/--gw/--victim`
set it and are verified by read-back before anything is captured. The defaults
below are a two-board direct-cable subnet, which is the arrangement the demos
describe; nothing in the firmware prefers them.

SERIAL PORT. `--port` takes the device's virtual COM port. With no `--port` and
no `ETHTIMER_PORT` in the environment, it picks the one attached ST-LINK VCP and
refuses to guess when there are several -- a capture driven at the wrong board
is a capture of nothing, and "no such port" is a far cheaper error than twenty
minutes of timeouts. `--ports` lists what it can see.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys

from . import campaign, target as _target
from .device import Device

_PKG = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_PKG)
_DEMOS = os.path.join(_ROOT, "demos")

#: name -> the file that defines and registers it. Kept as a table rather than a
#: scan so that `--list` is honest about what exists without importing every
#: demo, and so a typo names the file it could not find.
_BUILTIN = {
    "dns": os.path.join(_DEMOS, "dns", "dns_target.py"),
    "snmpv3": os.path.join(_DEMOS, "snmpv3", "snmpv3_target.py"),
    "snmpv3-harmony": os.path.join(_DEMOS, "snmpv3",
                                   "snmpv3_harmony_target.py"),
    "oscore": os.path.join(_DEMOS, "oscore", "oscore_target.py"),
    "tls": os.path.join(_DEMOS, "tls", "tls_target.py"),
}


def _candidate_ports():
    """(device, description, serial) for every serial port this machine sees."""
    try:
        from serial.tools import list_ports              # noqa: WPS433
    except ImportError:                                  # pragma: no cover
        return []
    return [(p.device, p.description or "", p.serial_number or "")
            for p in sorted(list_ports.comports(), key=lambda x: x.device)]


def _pick_port():
    """The one ST-LINK VCP attached -> `(port, None)`, else `(None, why)`.

    Guessing between two boards is the one thing not worth doing here: both
    answer, only one is the instrument on the cable, and the wrong one produces
    a full batch of timeouts that reads as a dead victim.
    """
    ports = _candidate_ports()
    if not ports:
        return None, ("no serial ports found. Attach the board and check its "
                      "probe enumerates; --ports lists what is visible.")
    likely = [p for p in ports
              if "stlink" in p[1].lower().replace("-", "").replace(" ", "")]
    pool = likely or ports
    if len(pool) == 1:
        return pool[0][0], None
    return None, ("%d candidate ports; pass --port (or set ETHTIMER_PORT):\n%s"
                  % (len(pool), "\n".join("  %-10s %s %s" % p for p in pool)))


def _load_module(path: str):
    """Import a target file by path, with its own import needs met.

    An adapter imports the protocol module next to it (`snmp3`, `oscore_py`). A
    module imported through `importlib` does NOT get its own directory on
    `sys.path` the way a script that is *run* does, so a bare `import snmp3`
    inside it fails with the adapter looking blameless. Both its directory and
    the repository root are added here explicitly.
    """
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise SystemExit("no such target module: %s" % path)
    for p in (os.path.dirname(path), _ROOT):
        if p not in sys.path:
            sys.path.insert(0, p)
    # Named for its directory as well as its file, so two demos' `target.py`
    # cannot collide in `sys.modules` -- which would silently hand the second
    # one the first one's module object.
    name = "ethtimer_demo_%s_%s" % (
        os.path.basename(os.path.dirname(path)),
        os.path.splitext(os.path.basename(path))[0])
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m ethtimer.cli",
        description="Capture Ethernet request/response timings with the "
                    "ethtimer instrument and check the captured bytes against "
                    "the measured device's real key.")
    ap.add_argument("--target", help="a shipped adapter: "
                                     + ", ".join(sorted(_BUILTIN)))
    ap.add_argument("--module", help="load an adapter from this .py file")
    ap.add_argument("--list", action="store_true",
                    help="show the shipped adapters and exit")
    ap.add_argument("--ports", action="store_true",
                    help="list the serial ports this machine can see and exit")
    ap.add_argument("--port", default=os.environ.get("ETHTIMER_PORT", ""),
                    help="the instrument's virtual COM port, e.g. COM10 or "
                         "/dev/ttyACM0. Defaults to $ETHTIMER_PORT, then to "
                         "the single attached ST-LINK VCP")
    ap.add_argument("--n", type=int, default=20000, help="exchanges to capture")
    ap.add_argument("--gap", type=int, default=200,
                    help="microseconds of idle between exchanges")
    ap.add_argument("--ip", default="192.168.7.20",
                    help="the instrument's own address")
    ap.add_argument("--mask", default="255.255.255.0")
    ap.add_argument("--gw", default="192.168.7.1")
    ap.add_argument("--victim", default="192.168.7.10",
                    help="the address of the device being measured")
    ap.add_argument("--out", default="", help="write the capture to this .npz")
    ap.add_argument("--spot-check", type=int, default=256,
                    help="exchanges to verify against the real key; 0 = all")
    ap.add_argument("--no-link-check", action="store_true",
                    help="allow a link that is not 100 Mbit full duplex. A "
                         "10 Mbit link reads as up and serialises a frame ten "
                         "times more slowly, which erases the structure this "
                         "measures, so this is for diagnosing the link and "
                         "never for collecting")
    a = ap.parse_args(argv)

    if a.ports:
        for row in _candidate_ports():
            print("%-10s %-50s %s" % row)
        return 0

    if a.list:
        for k in sorted(_BUILTIN):
            mark = "" if os.path.exists(_BUILTIN[k]) else "   (not present)"
            print("%-16s %s%s" % (k, os.path.relpath(_BUILTIN[k], _ROOT), mark))
        return 0

    if not (a.target or a.module):
        ap.error("one of --target or --module is required (see --list)")

    path = a.module or _BUILTIN.get(a.target)
    if path is None:
        ap.error("unknown target %r; --list shows the shipped ones" % a.target)
    _load_module(path)

    name = a.target if a.target in _target.known() else None
    if name is None:
        got = _target.known()
        if len(got) == 1:
            name = got[0]
        else:
            ap.error("%s registered %s; pass --target to choose"
                     % (os.path.basename(path), ", ".join(got) or "nothing"))
    tgt = _target.get(name)()

    port = a.port
    if not port:
        port, why = _pick_port()
        if port is None:
            raise SystemExit("no --port given and " + why)
        print("using %s" % port)

    with Device(port) as d:
        d.set_net(ip=a.ip, mask=a.mask, gw=a.gw, victim=a.victim)
        out = ""
        if a.out:
            out = a.out if os.path.isabs(a.out) else os.path.abspath(a.out)
        res = campaign.run(d, tgt, n=a.n, gap_us=a.gap,
                           check_link=not a.no_link_check,
                           spot_check=a.spot_check, out=out)

    print(res.summary())
    if out:
        print("wrote %s" % res.save(out))
    return 0 if res.verified else 2


if __name__ == "__main__":
    raise SystemExit(main())
