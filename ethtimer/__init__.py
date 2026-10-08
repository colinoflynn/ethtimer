# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""ethtimer -- host interface to the Ethernet request/response timing instrument.

The firmware is in `../../firmware`; the two halves are one project and the
protocol is defined once, in `firmware/inc/ethtimer.h`, with `proto.py`
mirroring it and `tests/test_proto.py` holding the two together.

Three layers, and the boundary between the first two is the point of the whole
module:

* `proto` / `device` -- the wire protocol and `Device`. Knows UDP, TCP and
  "bytes"; knows nothing about any application protocol.
* `target` -- what a protocol adapter must supply. Adapters live with their
  demo, not here.
* `campaign` / `cli` -- the parts that are the same whatever the protocol: the
  probe exchange that sizes the window, the batching, the diagnostics, the
  acceptance check against the victim's real key, and the saved file layout.
"""
from .device import Batch, Config, Device, DeviceError, Info   # noqa: F401
from . import proto                                            # noqa: F401
from . import target                                           # noqa: F401
from . import campaign                                         # noqa: F401
from .target import Blocks, Target                             # noqa: F401

__all__ = ["Device", "Batch", "Config", "Info", "DeviceError", "proto",
           "target", "campaign", "Target", "Blocks"]
