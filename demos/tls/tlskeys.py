# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""Derive the TLS 1.2 record keys, for the acceptance check only.

Taking the measurement does not need them: under CBC every record block gives a
known (input, output) pair on the wire. Checking the measurement does -- the
acceptance check encrypts the captured input under the real key and compares,
and without a key there is nothing to compare against.

Python's ssl can write the master secret to an NSS key log
(`SSLContext.keylog_filename`). Combined with the two handshake randoms -- which
the host sees on the wire, because it is the one driving TLS -- that is enough
to run RFC 5246's PRF and split the key block.

For TLS_RSA_WITH_AES_128_CBC_SHA256 the key block is

    client_write_MAC (32) || server_write_MAC (32) ||
    client_write_key (16) || server_write_key (16)

and the device encrypts its responses with the **server** write key, which is
the one the acceptance check needs.
"""
from __future__ import annotations

import hashlib
import hmac


def p_hash(secret: bytes, seed: bytes, length: int) -> bytes:
    """RFC 5246 s5 P_SHA256."""
    out = b""
    a = seed
    while len(out) < length:
        a = hmac.new(secret, a, hashlib.sha256).digest()
        out += hmac.new(secret, a + seed, hashlib.sha256).digest()
    return out[:length]


def prf(secret: bytes, label: bytes, seed: bytes, length: int) -> bytes:
    return p_hash(secret, label + seed, length)


def key_block(master_secret: bytes, client_random: bytes, server_random: bytes,
              mac_len=32, key_len=16):
    kb = prf(master_secret, b"key expansion", server_random + client_random,
             2 * mac_len + 2 * key_len)
    off = 2 * mac_len
    return {
        "client_write_key": kb[off:off + key_len],
        "server_write_key": kb[off + key_len:off + 2 * key_len],
    }


def read_keylog(path, client_random: bytes):
    """Master secret for this connection from an NSS key log."""
    want = client_random.hex()
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) == 3 and parts[0] == "CLIENT_RANDOM" \
                    and parts[1].lower() == want:
                return bytes.fromhex(parts[2])
    return None


def hello_random(record: bytes):
    """The 32-byte random out of a ClientHello or ServerHello record.

    Layout: 5-byte record header, 1-byte handshake type, 3-byte length,
    2-byte version, then the random.
    """
    if len(record) < 43:
        return None
    return record[11:43]
