# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""Hold `ethtimer.aes` against FIPS-197's own vectors.

No hardware, no toolchain. This matters more than its size suggests: every
capture's verdict is `E(K, aes_in) == aes_out`, so an AES that is subtly wrong
turns every acceptance check into a false negative -- and a false negative
there reads exactly like a window landing in the wrong place, which is the thing
the check exists to find. A wrong AES would send the reader to the firmware.

WHAT THIS DOES NOT COVER. Decryption (there is none), key sizes other than 128
bits (there are none), and any mode (`aes128_ecb` is a block permutation and the
adapters build their own CFB/CTR/CBC relations from it). It also says nothing
about speed; the vectorisation is checked only for *agreeing* with the
block-at-a-time answer.
"""
from __future__ import annotations

import numpy as np
import pytest

from ethtimer.aes import SBOX, aes128_ecb, key_expansion


def _enc(key_hex: str, pt_hex: str) -> str:
    block = np.frombuffer(bytes.fromhex(pt_hex), np.uint8).reshape(1, 16)
    return aes128_ecb(bytes.fromhex(key_hex), block).tobytes().hex()


def test_fips197_c1():
    """FIPS-197 Appendix C.1, the AES-128 example."""
    assert _enc("000102030405060708090a0b0c0d0e0f",
                "00112233445566778899aabbccddeeff") == \
        "69c4e0d86a7b0430d8cdb78070b4c55a"


def test_fips197_appendix_b():
    """FIPS-197 Appendix B, the worked cipher example."""
    assert _enc("2b7e151628aed2a6abf7158809cf4f3c",
                "3243f6a8885a308d313198a2e0370734") == \
        "3925841d02dc09fbdc118597196a0b32"


@pytest.mark.parametrize("key,pt,ct", [
    # NIST SP 800-38A F.1.1, ECB-AES128.Encrypt, all four blocks.
    ("2b7e151628aed2a6abf7158809cf4f3c",
     "6bc1bee22e409f96e93d7e117393172a", "3ad77bb40d7a3660a89ecaf32466ef97"),
    ("2b7e151628aed2a6abf7158809cf4f3c",
     "ae2d8a571e03ac9c9eb76fac45af8e51", "f5d3d58503b9699de785895a96fdbaaf"),
    ("2b7e151628aed2a6abf7158809cf4f3c",
     "30c81c46a35ce411e5fbc1191a0a52ef", "43b1cd7f598ece23881b00e3ed030688"),
    ("2b7e151628aed2a6abf7158809cf4f3c",
     "f69f2445df4f9b17ad2b417be66c3710", "7b0c785e27e8ad3f8223207104725dd4"),
])
def test_sp800_38a_ecb(key, pt, ct):
    assert _enc(key, pt) == ct


def test_sbox_is_the_real_sbox():
    """Generated, so the generator is what gets checked.

    Three fixed points of the table plus its permutation property. A table that
    is right at 0x00 and wrong at 0x7f would still encrypt *something*.
    """
    assert (int(SBOX[0x00]), int(SBOX[0x53]), int(SBOX[0xff])) == (0x63, 0xed, 0x16)
    assert sorted(int(b) for b in SBOX) == list(range(256))


def test_key_expansion_fips197_appendix_a1():
    """The last round key of FIPS-197 A.1.

    Round keys are flattened in the state's byte order, which is also the key's,
    so the expansion of words 40..43 reads straight off the appendix.
    """
    rk = key_expansion(bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c"))
    assert rk.shape == (11, 16)
    assert rk[0].tobytes().hex() == "2b7e151628aed2a6abf7158809cf4f3c"
    assert rk[10].tobytes().hex() == "d014f9a8c9ee2589e13f0cc8b6630ca6"


def test_vectorised_matches_one_at_a_time():
    """A stack of blocks encrypts the same as each block alone.

    The acceptance check calls this on `(n, k, 16)` and trusts the shape to
    survive, so both the leading shape and the per-block answer are checked.
    """
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    blocks = np.random.default_rng(1).integers(0, 256, (5, 3, 16), dtype=np.uint8)
    out = aes128_ecb(key, blocks)
    assert out.shape == blocks.shape
    for i in range(blocks.shape[0]):
        for j in range(blocks.shape[1]):
            one = aes128_ecb(key, blocks[i, j].reshape(1, 16))
            assert np.array_equal(one[0], out[i, j])


def test_rejects_a_bad_shape_and_a_bad_key():
    with pytest.raises(ValueError):
        aes128_ecb(b"\x00" * 16, np.zeros((2, 15), np.uint8))
    with pytest.raises(ValueError):
        aes128_ecb(b"\x00" * 24, np.zeros((1, 16), np.uint8))
