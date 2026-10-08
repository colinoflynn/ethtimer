# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""AES-128 ECB over a stack of blocks, for the acceptance check and nothing else.

`campaign` finishes a capture by encrypting the AES *input* it recorded under
the victim's real key and comparing against the AES *output* it recorded. That
is the whole reason this file exists, and it is why a reference implementation
is the right one to use here rather than a fast one: the check is a statement
about the capture, so the thing it is checked against has to be independently
correct and readable, not quick.

It is also why there is no dependency on a crypto library. `ethtimer` needs
`numpy` and `pyserial` to take a measurement; making it need a third package to
*verify* one would mean the verification gets skipped on the machine that is
missing it, and a capture nobody checked is how a window landing four bytes off
survives to the analysis.

Vectorised over the first axis, so a 20 000-record check is one call rather than
20 000. `aes128_ecb` is the only name a caller needs:

    >>> import numpy as np
    >>> blocks = np.frombuffer(bytes.fromhex("00112233445566778899aabbccddeeff"),
    ...                        np.uint8).reshape(1, 16)
    >>> key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    >>> aes128_ecb(key, blocks).tobytes().hex()
    '69c4e0d86a7b0430d8cdb78070b4c55a'

That is FIPS-197 C.1, and `tests/test_aes.py` checks it along with the rest of
the appendix.

STATE LAYOUT. The 16 bytes are held exactly as they arrive -- flat index `i` is
AES row `i % 4`, column `i // 4`, which is the column-major order FIPS-197 fills
the state in. So the input bytes need no permutation on the way in and the
output needs none on the way out, and `_SHIFT_ROWS` is expressed in those flat
indices.
"""
from __future__ import annotations

import numpy as np

__all__ = ["aes128_ecb", "key_expansion", "SBOX"]


def _build_sbox() -> np.ndarray:
    """The AES S-box, generated rather than written down.

    A 256-entry table pasted from somewhere else is a table nobody checked. This
    is the definition -- multiplicative inverse in GF(2^8) followed by the
    affine transform -- and it costs microseconds once at import.
    """
    # Log/antilog tables over the generator 3, used to invert.
    p = 1
    exp = np.zeros(256, np.uint8)
    log = np.zeros(256, np.uint8)
    for i in range(255):
        exp[i] = p
        log[p] = i
        p ^= (p << 1) ^ (0x1B if p & 0x80 else 0)
        p &= 0xFF
    exp[255] = exp[0]

    sbox = np.zeros(256, np.uint8)
    for x in range(256):
        inv = 0 if x == 0 else int(exp[255 - int(log[x])])
        y = inv
        for _ in range(4):
            inv = ((inv << 1) | (inv >> 7)) & 0xFF
            y ^= inv
        sbox[x] = y ^ 0x63
    return sbox


SBOX = _build_sbox()

_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime_table(n: int) -> np.ndarray:
    """`b -> b * n` in GF(2^8), as a 256-entry lookup."""
    out = np.zeros(256, np.uint8)
    for b in range(256):
        a, m, r = b, n, 0
        while m:
            if m & 1:
                r ^= a
            a = ((a << 1) ^ (0x1B if a & 0x80 else 0)) & 0xFF
            m >>= 1
        out[b] = r
    return out


_X2 = _xtime_table(2)
_X3 = _xtime_table(3)

# ShiftRows on the flat state: new[r, c] = old[r, (c + r) % 4], and flat index
# j is row j % 4, column j // 4.
_SHIFT_ROWS = np.array(
    [(j % 4) + 4 * ((j // 4 + (j % 4)) % 4) for j in range(16)], dtype=np.intp)


def key_expansion(key16: bytes) -> np.ndarray:
    """The 11 AES-128 round keys as `(11, 16)` uint8, in the state's byte order."""
    key16 = bytes(key16)
    if len(key16) != 16:
        raise ValueError("AES-128 takes a 16-byte key, got %d" % len(key16))
    words = [list(key16[4 * i:4 * i + 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(words[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]                       # RotWord
            t = [int(SBOX[b]) for b in t]           # SubWord
            t[0] ^= _RCON[i // 4 - 1]               # Rcon
        words.append([words[i - 4][j] ^ t[j] for j in range(4)])
    flat = bytes(b for w in words for b in w)
    return np.frombuffer(flat, np.uint8).reshape(11, 16).copy()


def _mix_columns(state: np.ndarray) -> np.ndarray:
    """`(n, 16)` uint8 -> MixColumns of it.

    `reshape(-1, 4, 4)` on the flat layout gives `[n, column, row]`, because
    flat index j is row `j % 4` of column `j // 4` -- so the last axis is the
    one MixColumns mixes.
    """
    s = state.reshape(-1, 4, 4)
    a0, a1, a2, a3 = s[:, :, 0], s[:, :, 1], s[:, :, 2], s[:, :, 3]
    out = np.empty_like(s)
    out[:, :, 0] = _X2[a0] ^ _X3[a1] ^ a2 ^ a3
    out[:, :, 1] = a0 ^ _X2[a1] ^ _X3[a2] ^ a3
    out[:, :, 2] = a0 ^ a1 ^ _X2[a2] ^ _X3[a3]
    out[:, :, 3] = _X3[a0] ^ a1 ^ a2 ^ _X2[a3]
    return out.reshape(state.shape)


def aes128_ecb(key16: bytes, blocks) -> np.ndarray:
    """ECB-encrypt `(..., 16)` uint8 under `key16`. Shape is preserved.

    Any leading shape is accepted and returned, so the `(n, k, 16)` that a
    `Blocks` carries goes through without the caller reshaping it.
    """
    a = np.asarray(blocks, np.uint8)
    if a.ndim < 1 or a.shape[-1] != 16:
        raise ValueError("blocks must be (..., 16) uint8, got %s" % (a.shape,))
    shape = a.shape
    state = a.reshape(-1, 16).astype(np.uint8, copy=True)

    rk = key_expansion(key16)
    state ^= rk[0]
    for rnd in range(10):
        state = SBOX[state]
        state = state[:, _SHIFT_ROWS]
        if rnd != 9:
            state = _mix_columns(state)
        state ^= rk[rnd + 1]
    return state.reshape(shape)
