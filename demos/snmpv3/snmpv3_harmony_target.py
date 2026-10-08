# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""A Harmony 3 SNMPv3 agent as an `ethtimer` target.

    python -m ethtimer.cli --target snmpv3-harmony --n 20000 \\
        --ip 192.168.100.20 --gw 192.168.100.1 --victim 192.168.100.10

Same protocol, same window, same acceptance check as
[`snmpv3_target.py`](snmpv3_target.py) -- a different *victim*, on a
PIC32MZ2048EFH144 running Microchip's `snmpv3_nvm_mpfs`. Everything that differs
is credentials and the localization hash, which is why this is a subclass and
not a second adapter: if it needed more than this, a stack's quirks would be
leaking into the instrument.

| | lwIP agent | Harmony agent |
|---|---|---|
| user | `victim` | `microchip` |
| localization | SHA-1 | **MD5** |
| subnet | 192.168.7.x | 192.168.100.x |

**NOT YET RUN THROUGH THIS ADAPTER.** The figures quoted for this device --
20 000 exchanges, 0 timeouts, 0 short, 402/s, median 1 739.69 us, with
`E(K, C1) == ks2` holding on every block checked -- were taken by driving
`Device` directly with these same credentials and the same check, before
`campaign` existed. The protocol work below is shared with the lwIP adapter
rather than copied, so there is little left to go wrong; what has not
happened is a capture through `campaign` against this device. Treat it as
untested until one has been.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from ethtimer.target import register                           # noqa: E402
from snmpv3_target import Snmpv3Target            # noqa: E402

USER = b"microchip"
AUTH_PASS = b"auth12345"
PRIV_PASS = b"priv12345"
#: Harmony localizes with MD5 where lwIP's agent uses SHA-1. This is the whole
#: difference in the protocol work, and getting it wrong yields a key that
#: derives cleanly and decrypts nothing.
ALGO = "md5"


@register
class HarmonySnmpv3Target(Snmpv3Target):
    name = "snmpv3-harmony"

    def __init__(self):
        super().__init__(user=USER, auth_pass=AUTH_PASS, priv_pass=PRIV_PASS,
                         algo=ALGO)
