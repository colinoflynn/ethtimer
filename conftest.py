# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""Put the repository root on `sys.path` so `import ethtimer` works uninstalled.

`pip install -e .` also works and is what the README suggests, but a clone that
has only had `pip install numpy pyserial` run in it should still be able to run
the tests -- the host-side tests are the first thing anyone checks after a
clone, and failing them on a packaging step teaches nothing about the protocol.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
