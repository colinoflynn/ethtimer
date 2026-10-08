# `tls` — timing an HTTPS request over a held-open connection

Opens one TLS 1.2 session, then sends HTTP `GET` requests inside it, times each
turnaround, and keeps the explicit IV plus fifteen ciphertext blocks of each
response record.

Then it checks itself: `E(K, P_i ^ C_(i-1))` must equal `C_i` on every block,
under the server's real write key — **fifteen pairs per exchange**, so a
5 000-exchange capture is 75 000 checked blocks.

This is the demo that exercises the instrument's **TCP** path: `TCP_CONNECT`,
`RELAY` and a banked capture on a stream rather than a datagram.

## What the other end has to be

An HTTPS server with:

* **TLS 1.2** and **`TLS_RSA_WITH_AES_128_CBC_SHA256`** (`AES128-SHA256`). The
  adapter asks for that suite explicitly so a mismatch is an error here rather
  than a quiet renegotiation into something it cannot parse. CBC is not a
  recommendation; it is what makes both ends of each AES call visible on the
  wire, and therefore what makes the capture checkable;
* `keep-alive`, since one session carries the whole run;
* a **constant** response to `GET /index.html` — the reference serves a fixed
  255-byte page, which is fifteen whole blocks of known plaintext plus padding.
  `prepare()` fetches it twice and refuses to continue if the two differ;
* a certificate. The adapter never validates it, so a self-signed one is fine —
  [`make_certs.py`](make_certs.py) generates one to embed in the server.

The reference device is a NUCLEO-F429ZI running lwIP's httpd over `altcp_tls`
with mbedTLS.

## Run it

```bash
python -m ethtimer.cli --target tls --n 5000 \
    --ip 192.168.7.20 --gw 192.168.7.1 --victim 192.168.7.10 \
    --out tls.npz
```

```
tls: response record 277 B, page 255 B, constant across two requests
tls: server write key 7f…  (ground truth, acceptance check only)
tls: 5000 records of 5000 requested, 0 timeouts, 0 short, 0 dropped, 156/s,
     median 799.5 us
  E(K, aes_in) == aes_out on 75000/75000 blocks (5000 exchanges x 15)
```

## Why TLS runs on the host and the board is a relay

A TLS client on the board would hand the application **plaintext**. What this
measures is the turnaround around the **ciphertext that is actually on the
wire**, so the side that drives TLS has to be the side that holds the record
bytes. The board therefore carries a TCP connection and a cycle counter and
nothing else, which is exactly what the instrument's TCP transport is.

Python's `ssl` is driven over a `MemoryBIO` pair, so the host produces and
consumes records without ever owning a socket to the server.

## Two phases, two mechanisms

* The **handshake** cannot be pre-generated: every record depends on the one
  before it. It goes through `Device.relay()`, one exchange per host round trip,
  which is what `relay` mode exists for.
* The **bulk phase** can be. The host holds the TLS write state, so it produces
  a run of request records — each with its own sequence number and explicit IV —
  without waiting for any response, and the board walks the bank. A serial round
  trip per exchange holds a relay to tens of exchanges a second; banking them
  reaches the device's own limit, which on the reference device is 156/s.

The read side is deliberately **not** advanced during the bulk phase: the board
returns only a window of each response, and the pair is formed from the window's
own bytes without decrypting anything.

## Fifteen pairs per exchange

A TLS 1.2 CBC record is `header || explicit IV || C_1 .. C_n` with
`C_i = E(K, P_i ^ C_(i-1))`, and `C_0` is the explicit IV, sent in clear. So for
every block of known plaintext:

```
aes_in[i]  = P_i ^ C_(i-1)
aes_out[i] = C_i
```

Fifteen blocks of the response are fully known, and the device encrypted all of
them inside **one** measured turnaround. Using all fifteen rather than the first
is the difference between a thirteen-hour capture and a one-hour one, which is
why `k > 1` is a first-class case in `Blocks` rather than an afterthought — and
why `ET_MAX_WIN` has to hold 16 blocks.

## The key log

The acceptance check needs the server write key, and the adapter gets it the
same way a browser's developer tools would: Python's `ssl` writes the master
secret to an NSS key log (`SSLContext.keylog_filename`), and
[`tlskeys.py`](tlskeys.py) runs RFC 5246's PRF over it with the two handshake
randoms — which the host saw on the wire, being the side driving TLS — to split
the key block.

Taking the measurement never reads it. If the key log is missing or has no
master secret for this session, the capture is reported as **NOT CHECKED** and
is not silently passed.

The log is written to the system temporary directory by default and contains
material that decrypts the session. It is not something to keep.

## One session, and what happens if it drops

One session carries the whole run, by design: every exchange has to be protected
by the same record key. A session that dies at minute eighteen of twenty-one
therefore costs the run — so `campaign` writes each batch to a `.part` file as
it lands and leaves it there when the run does not reach its end. Nothing here
resumes the session; `load_partial()` reads back what was collected.

## Files

| | |
|---|---|
| [`tls_target.py`](tls_target.py) | the adapter: handshake over `relay`, bulk over `bank` |
| [`tlskeys.py`](tlskeys.py) | RFC 5246 PRF and key-block split, for the acceptance check only |
| [`make_certs.py`](make_certs.py) | generate a self-signed certificate to embed in the server being measured. Needs `cryptography`; nothing else here does |
