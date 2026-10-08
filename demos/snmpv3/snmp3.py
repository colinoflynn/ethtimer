# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""SNMPv3 USM: just enough to be the client half, and to be checked.

The reference device for this demo runs lwIP's stock SNMPv3 agent with mbedTLS
underneath. This module is the other end: it builds an authPriv GET, parses the
reply, and reproduces the RFC 3414 key localization, so the demo carries no
runtime dependency on a large SNMP stack. It is deliberately small -- SNMPv3's
wire format is a fixed handful of BER structures.

WHAT THE CAPTURE GETS OUT OF IT: AES-128-CFB128 (RFC 3826) keystream. Block 1
is E(K, IV) with IV = engineBoots || engineTime || privParam, and block n after
that is E(K, C_{n-1}); the ciphertext is XORed with it. So

    keystream_n = P_n ^ C_n

is an AES *output* block whose *input* `C_{n-1}` is in clear on the wire. P is
the scopedPDU, which for a fixed request against a fixed MIB is byte-for-byte
constant, so the keystream is recoverable without the key -- which is what makes
the pair, and therefore the acceptance check, possible.
"""
from __future__ import annotations

import hashlib
import hmac
import struct


def _len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def tlv(tag, payload):
    return bytes([tag]) + _len(len(payload)) + payload


def integer(v):
    """BER INTEGER. Note the sign byte: a value whose top bit is set needs a
    leading 0x00 or it decodes as negative. Getting this wrong made msgMaxSize
    65507 encode as -29, and the agent dropped the message without a Report."""
    if v == 0:
        return tlv(0x02, bytes(1))
    b = v.to_bytes((v.bit_length() + 7) // 8, "big")
    if b[0] & 0x80:
        b = bytes(1) + b
    return tlv(0x02, b)


def octets(b):
    return tlv(0x04, b)


def oid(parts):
    out = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        if p < 0x80:
            out += bytes([p])
        else:
            chunk = []
            while p:
                chunk.insert(0, (p & 0x7F) | 0x80)
                p >>= 7
            chunk[-1] &= 0x7F
            out += bytes(chunk)
    return tlv(0x06, out)


def parse(buf, off=0):
    """(tag, value_bytes, next_offset) for one TLV."""
    tag = buf[off]
    n = buf[off + 1]
    off += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(buf[off:off + k], "big")
        off += k
    return tag, buf[off:off + n], off + n


def children(buf):
    out, off = [], 0
    while off < len(buf):
        tag, val, off = parse(buf, off)
        out.append((tag, val))
    return out


def as_int(b):
    return int.from_bytes(b, "big")


def password_to_key(password: bytes, engine_id: bytes, algo="sha") -> bytes:
    """RFC 3414 A.2: hash 1 MiB of the repeated passphrase, then bind the digest
    to the engine ID. This is what makes the privacy key a device-lifetime
    secret -- it is a function of the passphrase and the engine, nothing
    per-session -- which is the property this whole target selection rests on.
    """
    h = hashlib.sha1() if algo == "sha" else hashlib.md5()
    reps, rem = divmod(1048576, len(password))
    h.update(password * reps + password[:rem])
    digest = h.digest()
    h2 = hashlib.sha1() if algo == "sha" else hashlib.md5()
    h2.update(digest + engine_id + digest)
    return h2.digest()


def _aes_ecb(key, block):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    c = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return c.update(block) + c.finalize()


def cfb_iv(boots, etime, priv_param):
    return struct.pack(">II", boots, etime) + priv_param


def cfb_crypt(key, iv, data):
    """AES-128-CFB128, encrypt direction. CFB decryption of a ciphertext uses
    the same keystream, so this doubles as the decryptor when fed ciphertext."""
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        ks = _aes_ecb(key, prev)
        chunk = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(chunk, ks))
        prev = bytes(out[i:i + 16])
    return bytes(out)


def cfb_decrypt(key, iv, data):
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        ks = _aes_ecb(key, prev)
        chunk = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(chunk, ks))
        prev = chunk
    return bytes(out)


def keystream_blocks(plaintext, ciphertext, n=2):
    """The AES output blocks a capture yields: P ^ C, block by block."""
    return [bytes(a ^ b for a, b in zip(plaintext[i * 16:(i + 1) * 16],
                                        ciphertext[i * 16:(i + 1) * 16]))
            for i in range(n)]


FLAG_AUTH, FLAG_PRIV, FLAG_REPORT = 0x01, 0x02, 0x04


def scoped_pdu(engine_id, request_id, oids, pdu_tag=0xA0):
    vbs = b"".join(tlv(0x30, oid(o) + tlv(0x05, b"")) for o in oids)
    pdu = tlv(pdu_tag,
              integer(request_id) + integer(0) + integer(0) + tlv(0x30, vbs))
    return tlv(0x30, octets(engine_id) + octets(b"") + pdu)


def build_message(engine_id, boots, etime, user, auth_key, priv_key,
                  msg_id, request_id, oids, priv_param=b"\x00" * 8,
                  flags=FLAG_AUTH | FLAG_PRIV | FLAG_REPORT, algo="sha"):
    """A complete authPriv GET. Returns (packet, scoped_pdu_plaintext).

    `algo` selects the authentication HMAC, "sha" (HMAC-SHA1, RFC 3414 §7) or
    "md5" (HMAC-MD5, §6). It must match whatever `password_to_key` derived the
    auth key with.

    THIS USED TO BE SHA1-ONLY, AND THAT WAS A LATENT LIMITATION RATHER THAN A
    CHOICE: `password_to_key` above already took an `algo` argument, so a caller
    could localize an MD5 key and then have it MAC'd with SHA1, silently, and
    get nothing back but authenticationFailure. The lwIP victim this module was
    written against uses SHA1; Microchip's Harmony demo ships
    `SNMPV3_HMAC_MD5`, which is what surfaced it.
    """
    scoped = scoped_pdu(engine_id, request_id, oids)
    if flags & FLAG_PRIV:
        iv = cfb_iv(boots, etime, priv_param)
        body = octets(cfb_crypt(priv_key, iv, scoped))
    else:
        body = scoped
        priv_param = b""

    blank = b"\x00" * 12
    usm = tlv(0x30, octets(engine_id) + integer(boots) + integer(etime) +
              octets(user) + octets(blank if flags & FLAG_AUTH else b"") +
              octets(priv_param))
    header = tlv(0x30, integer(msg_id) + integer(65507) +
                 octets(bytes([flags])) + integer(3))
    msg = tlv(0x30, integer(3) + header + octets(usm) + body)

    if flags & FLAG_AUTH:
        digestmod = hashlib.sha1 if algo == "sha" else hashlib.md5
        mac = hmac.new(auth_key, msg, digestmod).digest()[:12]
        i = msg.find(blank)
        msg = msg[:i] + mac + msg[i + 12:]
    return msg, scoped


def build_discovery(msg_id=1):
    """The standard engine-discovery probe: no user, no auth, no privacy. The
    agent answers with a Report carrying its engine ID, boots and time."""
    scoped = tlv(0x30, octets(b"") + octets(b"") +
                 tlv(0xA0, integer(1) + integer(0) + integer(0) + tlv(0x30, b"")))
    usm = tlv(0x30, octets(b"") + integer(0) + integer(0) +
              octets(b"") + octets(b"") + octets(b""))
    header = tlv(0x30, integer(msg_id) + integer(65507) +
                 octets(bytes([FLAG_REPORT])) + integer(3))
    return tlv(0x30, integer(3) + header + octets(usm) + scoped)


def parse_message(pkt):
    """-> dict with engine_id, boots, time, user, priv_param, body, encrypted."""
    _, top, _ = parse(pkt)
    kids = children(top)
    _, header = kids[1]
    _, secparm = kids[2]
    body_tag, body = kids[3]
    hk = children(header)
    flags = hk[2][1][0] if hk[2][1] else 0
    inner = parse(secparm)[1]
    sk = children(inner)
    return {
        "flags": flags,
        "engine_id": sk[0][1],
        "boots": as_int(sk[1][1]),
        "time": as_int(sk[2][1]),
        "user": sk[3][1],
        "auth_param": sk[4][1],
        "priv_param": sk[5][1],
        "body": body,
        "encrypted": body_tag == 0x04,
    }


def _content_start(pkt, off=0):
    """Offset of a TLV's value, i.e. past its tag and length bytes."""
    n = pkt[off + 1]
    return off + 2 + ((n & 0x7F) if (n & 0x80) else 0)


def ciphertext_offset(pkt):
    """Byte offset of the encrypted scopedPDU inside the datagram.

    The instrument keeps a fixed window of each reply, so this is worked out
    once per capture. It is *mostly* stable for a fixed request -- but see
    `snmpv3_target.py`, where `engineTime` growing by a BER byte moves it
    mid-capture, which is why the adapter keeps a landmark rather than trusting
    this number for a whole run.
    """
    off = _content_start(pkt)                  # into the outer SEQUENCE
    for _ in range(3):                         # version, msgGlobalData, secparams
        _, _, off = parse(pkt, off)
    return _content_start(pkt, off)            # into the encryptedPDU OCTET STRING
