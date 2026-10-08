# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""Hold `oscore_py.py` against RFC 8613's own test vectors.

No hardware, no toolchain. These are the same vectors `uoscore-uedhoc` checks
itself against (`test_vectors/oscore_test_vectors.c`, the `T1__` set), which is
what makes a disagreement a bug here rather than a difference of reading.

    python -m pytest tests/test_oscore_py.py -q

WHAT THIS DOES NOT COVER. The key schedule, the nonce, the AAD and one full
request/response round trip on the C.1 context -- that is, everything the wire
format depends on. It says nothing about a server's replay window, nothing
about whether a bank of requests is accepted in order, and nothing about the
timing path; those need the board and live in the acceptance capture.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
#: The OSCORE client lives with its demo, not with its test, so the demo's
#: directory goes on the path explicitly -- pytest puts the TEST's directory
#: there, which is not the same thing.
_DEMO = os.path.join(os.path.dirname(_HERE), "demos", "oscore")
if _DEMO not in sys.path:
    sys.path.insert(0, _DEMO)

import oscore_py as O                                       # noqa: E402

# RFC 8613 Appendix C.1.1 -- client context.
MASTER_SECRET = bytes.fromhex("0102030405060708090a0b0c0d0e0f10")
MASTER_SALT = bytes.fromhex("9e7ca92223786340")
SENDER_ID = b""
RECIPIENT_ID = b"\x01"

SENDER_KEY = bytes.fromhex("f0910ed7295e6ad4b54fc793154302ff")
RECIPIENT_KEY = bytes.fromhex("ffb14e093c94c9cac9471648b4f98710")
COMMON_IV = bytes.fromhex("4622d4dd6d944168eefb54987c")

# Appendix C.4: the protected request for Partial IV 20, token 0x00003974,
# MID 0x5d1f, with a Uri-Host option the victim's client does not send.
C4_OSCORE_REQ = bytes.fromhex(
    "44025d1f00003974396c6f63616c686f737462091"
    "4ff612f1092f1776f1c1668b3825e")
C4_PIV = 0x14
C4_INNER = bytes.fromhex("01b3747631")          # GET, Uri-Path "tv1"

# Appendix C.7: the protected response to it.
C7_OSCORE_RESP = bytes.fromhex(
    "64445D1F0000397490FFDBAAD1E9A7E7B2A813D3C31524378303CDAFAE11910"
    "6".lower())


def test_key_schedule_matches_the_rfc():
    sk, rk, iv = O.derive(MASTER_SECRET, MASTER_SALT, SENDER_ID, RECIPIENT_ID)
    assert sk == SENDER_KEY
    assert rk == RECIPIENT_KEY
    assert iv == COMMON_IV


def test_nonce_matches_the_rfc():
    """C.4's nonce, which the RFC prints as 4622d4dd6d944168eefb549868."""
    n = O.nonce(COMMON_IV, SENDER_ID, C4_PIV)
    assert n.hex() == "4622d4dd6d944168eefb549868"


def test_aad_is_the_cose_enc_structure():
    a = O.aad(SENDER_ID, C4_PIV)
    # [ "Encrypt0", h'', h'8501810A40411440' ]
    assert a.hex() == "8368456e63727970743040488501810a40411440"


def test_request_encryption_reproduces_the_rfc_ciphertext():
    """The whole outgoing path, checked byte for byte against C.4.

    Built with the RFC's own token and MID so the comparison is of the
    protection, not of the framing this client happens to choose.
    """
    c = O.Client(MASTER_SECRET, MASTER_SALT, SENDER_ID, RECIPIENT_ID,
                 uri_path=b"tv1")
    req, piv = c.build_request(piv=C4_PIV, mid=0x5D1F, token=0x74)
    assert piv == C4_PIV
    # The RFC's message carries a 4-byte token and a Uri-Host option that this
    # client does not generate, so compare the part that the protection
    # produces: the OSCORE option value and the ciphertext.
    assert req[-13:] == C4_OSCORE_REQ[-13:], (
        "ciphertext differs: %s vs %s" % (req[-13:].hex(),
                                          C4_OSCORE_REQ[-13:].hex()))
    assert O.oscore_option(C4_PIV, SENDER_ID).hex() == "0914"


def test_response_decrypts_under_the_recipient_key():
    """C.7 opens with the recipient key and the REQUEST's nonce.

    This is the property the OSCORE demo rests on: the server protects its
    response with a key the client can name, under a nonce the client chose. So
    the client knows the AES input without knowing the key.
    """
    c = O.Client(MASTER_SECRET, MASTER_SALT, SENDER_ID, RECIPIENT_ID,
                 uri_path=b"tv1")
    pt = c.open_response(C7_OSCORE_RESP, C4_PIV)
    assert pt[0] == 0x45, "expected a 2.05 Content inner code, got %#x" % pt[0]


def test_keystream_is_e_of_a1_under_the_recipient_key():
    """`plaintext ^ ciphertext == E(K, A_1)` -- the identity the capture uses.

    If this holds, then recording `A_1` and `plaintext ^ ciphertext` gives a
    genuine AES (input, output) pair under the server's sender key, and the
    generic acceptance check applies to OSCORE unchanged.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    c = O.Client(MASTER_SECRET, MASTER_SALT, SENDER_ID, RECIPIENT_ID,
                 uri_path=b"tv1")
    pt = c.open_response(C7_OSCORE_RESP, C4_PIV)
    ct = c.response_ciphertext(C7_OSCORE_RESP)[:len(pt)]
    ks = bytes(a ^ b for a, b in zip(pt, ct))

    a1 = O.ccm_a1(O.nonce(COMMON_IV, SENDER_ID, C4_PIV))
    enc = Cipher(algorithms.AES(RECIPIENT_KEY), modes.ECB()).encryptor()
    expect = enc.update(a1) + enc.finalize()
    assert ks == expect[:len(ks)]


def test_every_request_gets_a_fresh_partial_iv():
    """The generator must never repeat: a repeat is rejected by the victim."""
    c = O.Client(MASTER_SECRET, MASTER_SALT, SENDER_ID, RECIPIENT_ID)
    pivs = [c.build_request()[1] for _ in range(64)]
    assert len(set(pivs)) == len(pivs)
    assert pivs == sorted(pivs)


def test_a1_differs_per_request():
    """Distinct Partial IVs must give distinct AES inputs.

    Without that, a 20 000-exchange capture holds one AES input 20 000 times and
    says nothing about the device that produced it.
    """
    c = O.Client(MASTER_SECRET, MASTER_SALT, SENDER_ID, RECIPIENT_ID)
    seen = set()
    for _ in range(64):
        _, piv = c.build_request()
        seen.add(O.ccm_a1(O.nonce(c.common_iv, c.sender_id, piv)))
    assert len(seen) == 64
