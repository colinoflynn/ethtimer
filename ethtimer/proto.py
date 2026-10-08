# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""The ethtimer wire protocol, mirroring `firmware/inc/ethtimer.h`.

The two definitions are kept in step by `host/tests/test_proto.py`, which parses
the header and asserts every constant here matches it. That test needs no
hardware and no toolchain, so a protocol change that forgets one side fails in
`pytest` rather than at the bench.

FRAME (both directions, little-endian):

    0      'E'
    1      'T'
    2      type        u8
    3..4   len         u16   payload length
    5..    payload     len bytes
    last2  crc16       u16   CCITT-FALSE over bytes [2 .. 5+len)

The CRC covers type and length as well as payload, so a corrupted length cannot
silently reframe the stream -- which the text protocol this replaces could not
detect at all.
"""
from __future__ import annotations

import struct

MAGIC = b"ET"
#: 2: the request bank, the TCP transport and RELAY. `Device` checks this
#: against GET_INFO before using anything a v1 board does not have, so a stale
#: board is named rather than left to fail three commands later.
PROTO_VERSION = 2

MAX_REQ = 512
MAX_WIN = 320
REC_OVERHEAD = 12
#: The largest frame the device will accept. A SET_BANK frame packs as many
#: entries as fit, because an ACK per entry would be a serial round trip per
#: entry.
MAX_FRAME_IN = 2048
BANK_RESET = 0x01

# ---- host -> device -------------------------------------------------------
CMD_PING = 0x01
CMD_GET_INFO = 0x02
CMD_SET_NET = 0x03
CMD_SET_TARGET = 0x04
CMD_SET_REQUEST = 0x05
CMD_SET_WINDOW = 0x06
CMD_GET_CONFIG = 0x07
CMD_RUN = 0x08
CMD_ONESHOT = 0x09
CMD_RESET = 0x0A
# ---- v2 ------------------------------------------------------------------
CMD_SET_BANK = 0x0B
CMD_TCP_CONNECT = 0x0C
CMD_TCP_CLOSE = 0x0D
CMD_RELAY = 0x0E

# ---- device -> host -------------------------------------------------------
RSP_ACK = 0x81
RSP_ERR = 0x82
RSP_INFO = 0x83
RSP_CONFIG = 0x84
RSP_BATCH_HDR = 0x85
RSP_BATCH_DATA = 0x86
RSP_BATCH_END = 0x87
RSP_ONESHOT = 0x88
RSP_RELAY = 0x89

ERR_NONE = 0
ERR_BAD_LEN = 1
ERR_BAD_ARG = 2
ERR_NO_REQUEST = 3
ERR_LINK_DOWN = 4
ERR_NOT_IMPL = 5
ERR_TOO_BIG = 6
ERR_WINDOW = 7
ERR_TCP = 8
ERR_BANK = 9

ERR_NAME = {
    ERR_NONE: "none", ERR_BAD_LEN: "bad length", ERR_BAD_ARG: "bad argument",
    ERR_NO_REQUEST: "no request loaded", ERR_LINK_DOWN: "link down",
    ERR_NOT_IMPL: "not implemented", ERR_TOO_BIG: "too big",
    ERR_WINDOW: "window does not fit the reply",
    ERR_TCP: "TCP: connect refused, or no open connection",
    ERR_BANK: "bank full, empty, or shorter than the run",
}

PROTO_UDP = 0
PROTO_TCP = 1

TCP_IDLE, TCP_CONNECTING, TCP_OPEN, TCP_CLOSED = 0, 1, 2, 3
TCP_NAME = {TCP_IDLE: "idle", TCP_CONNECTING: "connecting",
            TCP_OPEN: "open", TCP_CLOSED: "closed"}

LINK_DOWN, LINK_10H, LINK_10F, LINK_100H, LINK_100F = 0, 1, 2, 3, 4
LINK_NAME = {LINK_DOWN: "down", LINK_10H: "10H", LINK_10F: "10F",
             LINK_100H: "100H", LINK_100F: "100F"}


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: init 0xFFFF, poly 0x1021, no reflection, no xorout.

    Mirrors `et_crc16` in the firmware byte for byte. Kept as an explicit loop
    rather than a table so the two implementations can be read side by side.
    """
    c = 0xFFFF
    for b in data:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c


def encode(type_: int, payload: bytes = b"") -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError("payload too long for a u16 length")
    body = struct.pack("<BH", type_, len(payload)) + payload
    return MAGIC + body + struct.pack("<H", crc16(body))


class FrameError(Exception):
    pass


class Decoder:
    """Incremental frame decoder. Feed bytes, get whole frames out.

    Incremental because a batch arrives as a stream of chunk frames and the host
    should not have to know the total length in advance.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes):
        self._buf.extend(data)
        out = []
        while True:
            i = self._buf.find(MAGIC)
            if i < 0:
                # Keep the last byte: it may be a split 'E'.
                if len(self._buf) > 1:
                    del self._buf[:-1]
                break
            if i:
                del self._buf[:i]
            if len(self._buf) < 5:
                break
            (type_, n) = struct.unpack("<BH", self._buf[2:5])
            total = 5 + n + 2
            if len(self._buf) < total:
                break
            body = bytes(self._buf[2:5 + n])
            want = struct.unpack("<H", self._buf[5 + n:total])[0]
            got = crc16(body)
            del self._buf[:total]
            if want != got:
                raise FrameError("crc mismatch on type 0x%02x: want %04x got %04x"
                                 % (type_, want, got))
            out.append((type_, body[3:]))
        return out
