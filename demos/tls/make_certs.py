# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""Generate a self-signed certificate for the TLS server being measured.

Run once and embed the result in that server's build; the adapter never
validates it, so nothing here needs to be trusted or kept. It exists only so
that an HTTPS device under measurement has a certificate at all -- a device
certificate is what any real product ships with.

    python demos/tls/make_certs.py          # writes beside this script
"""
import datetime
import os

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_HERE = os.path.dirname(os.path.abspath(__file__))

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, u"192.168.1.10")])
cert = (x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2020, 1, 1))
        .not_valid_after(datetime.datetime(2040, 1, 1))
        .sign(key, hashes.SHA256()))

open(os.path.join(_HERE, "device_key.pem"), "wb").write(
    key.private_bytes(serialization.Encoding.PEM,
                      serialization.PrivateFormat.PKCS8,
                      serialization.NoEncryption()))
open(os.path.join(_HERE, "device_cert.pem"), "wb").write(
    cert.public_bytes(serialization.Encoding.PEM))
print("wrote device_key.pem and device_cert.pem in %s" % _HERE)
