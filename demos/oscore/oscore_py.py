# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""A host-side OSCORE client: enough of RFC 8613 to drive the victim from Python.

The instrument sends host-supplied bytes and knows nothing about any protocol,
so the client lives here rather than on the board. That is a gain rather than a
cost: a client that runs on the host knows the Partial IV of every request it
built, and therefore the CCM counter block `A_1` -- the AES **input** whose
output the response keystream is. A board-side client would hand back the
keystream alone, and the pair is what the acceptance check needs.

SCOPE. Client side, AES-CCM-16-64-128 with HKDF-SHA-256, no ID Context, no
Observe, no block-wise transfer, no key update. That is OSCORE's
mandatory-to-implement profile and exactly what the victim runs. It is not a
general OSCORE library and should not be mistaken for one.

Validated against RFC 8613 Appendix C.1 / C.4 / C.7 by `test_oscore_py.py`,
which is also the vector set `uoscore-uedhoc` checks itself against -- so a
disagreement is a bug here, not a difference of interpretation.
"""
from __future__ import annotations

import hashlib
import hmac
import struct

#: COSE algorithm identifier for AES-CCM-16-64-128 (RFC 9053). 13-byte nonce,
#: 8-byte tag, which is what fixes the shape of everything below.
ALG_AES_CCM_16_64_128 = 10
NONCE_LEN = 13
TAG_LEN = 8
KEY_LEN = 16

#: The OSCORE CoAP option number (RFC 8613 s2).
OPT_OSCORE = 9
OPT_URI_PATH = 11


# ------------------------------------------------------------------- CBOR ----
# Only the handful of encodings RFC 8613's `info` and AAD structures use. A
# dependency would be a back edge for four one-line functions, and these are
# deterministic by construction, which a general encoder is not.

def _cbor_uint(n: int) -> bytes:
    if n < 24:
        return bytes([n])
    if n < 256:
        return bytes([0x18, n])
    if n < 65536:
        return b"\x19" + struct.pack(">H", n)
    return b"\x1a" + struct.pack(">I", n)


def _cbor_head(major: int, n: int) -> bytes:
    b = _cbor_uint(n)
    return bytes([(major << 5) | b[0]]) + b[1:]


def _cbor_bstr(b: bytes) -> bytes:
    return _cbor_head(2, len(b)) + b


def _cbor_tstr(s: str) -> bytes:
    e = s.encode("utf-8")
    return _cbor_head(3, len(e)) + e


def _cbor_array(items) -> bytes:
    return _cbor_head(4, len(items)) + b"".join(items)


# -------------------------------------------------------------------- HKDF ---

def hkdf_sha256(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt or b"\x00" * 32, ikm, hashlib.sha256).digest()
    out, t, i = b"", b"", 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:length]


def _info(id_: bytes, id_context: bytes, alg: int, type_: str, length: int) -> bytes:
    """RFC 8613 s3.2.1: info = [ id, id_context, alg_aead, type, L ]."""
    return _cbor_array([
        _cbor_bstr(id_),
        _cbor_bstr(id_context) if id_context else b"\xf6",   # null
        _cbor_uint(alg),
        _cbor_tstr(type_),
        _cbor_uint(length),
    ])


def derive(master_secret: bytes, master_salt: bytes, sender_id: bytes,
           recipient_id: bytes, id_context: bytes = b"",
           alg: int = ALG_AES_CCM_16_64_128):
    """The three values a security context is: sender key, recipient key, common IV.

    Returns `(sender_key, recipient_key, common_iv)`. For a client talking to
    a server, the RECIPIENT key is the interesting one here: it is the key the
    server protects its responses with, and so the key whose AES the measured
    turnaround contains.
    """
    sk = hkdf_sha256(master_salt, master_secret,
                     _info(sender_id, id_context, alg, "Key", KEY_LEN), KEY_LEN)
    rk = hkdf_sha256(master_salt, master_secret,
                     _info(recipient_id, id_context, alg, "Key", KEY_LEN), KEY_LEN)
    iv = hkdf_sha256(master_salt, master_secret,
                     _info(b"", id_context, alg, "IV", NONCE_LEN), NONCE_LEN)
    return sk, rk, iv


# ------------------------------------------------------------------- nonce ---

def nonce(common_iv: bytes, id_piv: bytes, piv: int) -> bytes:
    """RFC 8613 s5.2.

    A RESPONSE that carries no Partial IV of its own reuses the REQUEST's nonce,
    so the same call serves both: pass the request's sender id and Partial IV.
    That is precisely why the client knows the AES input for a response it
    never generated -- it generated the request.
    """
    pv = piv.to_bytes(5, "big")
    idp = id_piv.rjust(NONCE_LEN - 6, b"\x00")
    s = bytes([len(id_piv)]) + idp + pv
    if len(s) != NONCE_LEN:
        raise ValueError("nonce assembled to %d bytes, not %d"
                         % (len(s), NONCE_LEN))
    return bytes(a ^ b for a, b in zip(s, common_iv))


def ccm_a1(n: bytes) -> bytes:
    """The first CCM counter block: `A_1 = flags || nonce || 0x0001`.

    RFC 3610. With a 13-byte nonce the length field L is 2, so the flags byte is
    L-1 = 1 and the counter is two bytes. `E(K, A_1)` is the keystream block
    that covers the first 16 bytes of plaintext -- the whole of this victim's
    response -- and it is the AES output a capture of that response holds.
    """
    if len(n) != NONCE_LEN:
        raise ValueError("A_1 needs a %d-byte nonce" % NONCE_LEN)
    return b"\x01" + n + b"\x00\x01"


# --------------------------------------------------------------------- AAD ---

def aad(request_kid: bytes, request_piv: int,
        alg: int = ALG_AES_CCM_16_64_128) -> bytes:
    """RFC 8613 s5.4: the COSE Enc_structure that CCM authenticates.

    external_aad = [ version, [alg], request_kid, request_piv, options ]
    Enc_structure = [ "Encrypt0", h'', external_aad ]

    `options` is always empty here: it carries only class-I options, and this
    profile defines none.
    """
    ext = _cbor_array([
        _cbor_uint(1),                                  # oscore_version
        _cbor_array([_cbor_uint(alg)]),                 # algorithms
        _cbor_bstr(request_kid),
        _cbor_bstr(request_piv.to_bytes(
            max(1, (request_piv.bit_length() + 7) // 8), "big")),
        _cbor_bstr(b""),                                # class-I options
    ])
    return _cbor_array([_cbor_tstr("Encrypt0"), _cbor_bstr(b""), _cbor_bstr(ext)])


# -------------------------------------------------------------------- CoAP ---

def _opt(number_delta: int, value: bytes) -> bytes:
    """One CoAP option. Deltas and lengths below 13 only, which covers this demo."""
    if number_delta > 12 or len(value) > 12:
        raise ValueError("this encoder handles only short deltas and lengths; "
                         "delta=%d len=%d" % (number_delta, len(value)))
    return bytes([(number_delta << 4) | len(value)]) + value


def oscore_option(piv: int, kid: bytes) -> bytes:
    """The OSCORE option value: flags, Partial IV, kid (RFC 8613 s6.1)."""
    pv = piv.to_bytes(max(1, (piv.bit_length() + 7) // 8), "big")
    if len(pv) > 5:
        raise ValueError("Partial IV does not fit in 5 bytes")
    flags = len(pv) | 0x08              # n = |piv|, k = 1 (kid present)
    return bytes([flags]) + pv + kid


def _ccm_encrypt(key: bytes, n: bytes, pt: bytes, ad: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESCCM
    return AESCCM(key, tag_length=TAG_LEN).encrypt(n, pt, ad)


def _ccm_decrypt(key: bytes, n: bytes, ct: bytes, ad: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESCCM
    return AESCCM(key, tag_length=TAG_LEN).decrypt(n, ct, ad)


class Client:
    """The client half of a security context, and the requests it generates.

    THE SEQUENCE NUMBER IS THE WHOLE POINT. `ssn` increments on every request,
    and the victim keeps a replay window that retires the values it has seen. A
    request may therefore be used exactly once: re-sending one is answered with
    an unprotected 4.00, not with the reply this measures. That is why the
    instrument grew a request BANK, and why this class is a generator rather
    than a `build_request()` that could be called twice by accident.
    """

    def __init__(self, master_secret: bytes, master_salt: bytes,
                 sender_id: bytes = b"", recipient_id: bytes = b"\x01",
                 id_context: bytes = b"", uri_path: bytes = b"t",
                 start_ssn: int = 0):
        self.sender_id = sender_id
        self.recipient_id = recipient_id
        self.uri_path = uri_path
        self.ssn = start_ssn
        self.mid = 0x1000
        self.token = 0
        (self.sender_key, self.recipient_key,
         self.common_iv) = derive(master_secret, master_salt, sender_id,
                                  recipient_id, id_context)

    # ---- outgoing ------------------------------------------------------
    def build_request(self, piv: int = None, mid: int = None, token: int = None):
        """One protected GET. Returns `(datagram, piv)`.

        `piv` is returned rather than stored because the caller needs it to
        compute the response's nonce, and because a bank of requests is built
        ahead of time and consumed later -- the client's own `ssn` will have
        moved on by then.
        """
        if piv is None:
            piv = self.ssn
            self.ssn += 1
        if mid is None:
            mid = self.mid & 0xFFFF
            self.mid += 1
        if token is None:
            token = self.token & 0xFF
            self.token += 1

        # The inner (protected) message: code, then class-E options.
        inner = bytes([0x01]) + _opt(OPT_URI_PATH, self.uri_path)
        n = nonce(self.common_iv, self.sender_id, piv)
        ct = _ccm_encrypt(self.sender_key, n, inner,
                          aad(self.sender_id, piv))

        # The outer message: a POST carrying the OSCORE option and the
        # ciphertext as payload (RFC 8613 s4.1.3.2).
        out = bytearray()
        out.append(0x41)                        # ver 1, CON, TKL 1
        out.append(0x02)                        # 0.02 POST
        out += struct.pack(">H", mid)
        out.append(token)
        out += _opt(OPT_OSCORE, oscore_option(piv, self.sender_id))
        out.append(0xFF)
        out += ct
        return bytes(out), piv

    # ---- incoming ------------------------------------------------------
    def response_ciphertext(self, datagram: bytes) -> bytes:
        """The protected payload of a response, ciphertext and tag."""
        i = datagram.find(b"\xff")
        if i < 0:
            raise ValueError("no payload marker in a %d-byte response: %s"
                             % (len(datagram), datagram.hex()))
        return datagram[i + 1:]

    def response_offset(self, datagram: bytes) -> int:
        """Where that payload starts, for the device's capture window."""
        i = datagram.find(b"\xff")
        if i < 0:
            raise ValueError("no payload marker in the response")
        return i + 1

    def open_response(self, datagram: bytes, piv: int) -> bytes:
        """Decrypt a response under the RECIPIENT key, for validation only.

        Not part of taking a measurement, which has no key. It is here so a
        capture can be checked against what the device really sent.
        """
        n = nonce(self.common_iv, self.sender_id, piv)
        return _ccm_decrypt(self.recipient_key, n,
                            self.response_ciphertext(datagram),
                            aad(self.sender_id, piv))
