# `oscore` — timing a protected CoAP exchange

*🤖WARNING🤖: This file LLM generated and may read oddly. Will eventually be human-edited
for that real-life touch and typos.*

Sends OSCORE-protected CoAP `GET` requests, each with a fresh Partial IV, times
each turnaround, and keeps one 16-byte block of the protected response.

Then it checks itself: `E(K, A_1)` must equal `plaintext ^ ciphertext` on every
pair, under the server's real sender key.

Needs `cryptography` for AES-CCM: `pip install -e ".[demos]"`. The instrument
does not.

## What the other end has to be

An OSCORE server (RFC 8613) with:

* **AES-CCM-16-64-128** with HKDF-SHA-256 — the mandatory-to-implement profile,
  and the only one [`oscore_py.py`](oscore_py.py) speaks;
* a master secret and salt you know. The reference device uses RFC 8613
  Appendix C.1's published test vector, which is why the "secret" in
  [`oscore_target.py`](oscore_target.py) is in the source;
* one resource with a **constant** response — the reference serves `GET /t`
  answering `{"v":22.54321}`, which with the inner code and the payload marker
  is exactly one AES block. `prepare()` checks the response matches the assumed
  plaintext byte for byte rather than taking it on trust;
* no ID Context, no Observe, no block-wise transfer, no key update.

The reference device is a NUCLEO-F429ZI running `uoscore-uedhoc` over TinyCrypt.

## Run it

```bash
python -m ethtimer.cli --target oscore --n 20000 \
    --ip 192.168.7.20 --gw 192.168.7.1 --victim 192.168.7.10 \
    --out oscore.npz
```

```
oscore: 20000 records of 20000 requested, 0 timeouts, 0 short, 0 bad dt,
        0 dropped, 510/s, median 937.4 us
  E(K, aes_in) == aes_out on 20000/20000 blocks
```

20 000 exchanges, 20 000 **distinct** AES inputs — one per exchange, because
every OSCORE request carries a fresh sequence number and the response's counter
block is derived from it. That 1:1 ratio is a property of the protocol; the
SNMPv3 demo's is a property of the agent's clock.

## Why the client runs on the host

The instrument sends host-supplied bytes and knows nothing about any protocol,
so the OSCORE client lives in [`oscore_py.py`](oscore_py.py). That is a gain
rather than a cost.

A client on the board could hand back the response keystream and nothing else. A
client on the host knows the Partial IV of every request it built, and therefore
knows the CCM counter block `A_1` — the AES **input** whose output that
keystream is. One of those is a pair and the other is half of one, and only the
pair can be checked.

## Why `bank`, and why a repeat is useless here

An OSCORE server keeps a replay window (RFC 8613 §7.4) and retires every Partial
IV it has seen, so a repeated request is answered with an unprotected 4.00
rather than the reply this measures.

It is worse than that: the response's CCM nonce **is the request's nonce**. If
the request never changes, neither does the AES input, and a capture of 20 000
exchanges holds one AES input 20 000 times. So each exchange needs a fresh
request, which is what the request bank is for — the host uploads a run of
distinct requests and the board plays it once.

**A bank is played once and never cycled.** Re-sending a sequence number the
server has retired gets a reject, and it arrives as a batch of rejects minutes
into a capture rather than at the first exchange.

### The replay window is not an error condition

A second run against a server that has not been reset will always meet it: the
client starts counting from zero and the server retired everything it saw last
time. A window only ever moves forward, so `prepare()` moves forward with it,
using nothing but the rejection the server already sends in clear. That is
recovery, not a workaround — but if you want the first run's numbers again,
reset the server.

## The pair

```
aes_in  = A_1 = 0x01 || nonce || 0x0001     the CCM counter block, derived from
                                            the Partial IV the client chose and
                                            sent in clear
aes_out = plaintext ^ ciphertext[0:16]      E(K, A_1) under the server's
                                            sender key
```

## Files

| | |
|---|---|
| [`oscore_target.py`](oscore_target.py) | the adapter |
| [`oscore_py.py`](oscore_py.py) | a host-side OSCORE client: key schedule, nonce, AAD, CCM. Scope is the mandatory-to-implement profile and nothing more; it is **not** a general OSCORE library |

[`../../tests/test_oscore_py.py`](../../tests/test_oscore_py.py) holds
`oscore_py.py` against RFC 8613 Appendix C.1 / C.4 / C.7 — the same vector set
`uoscore-uedhoc` checks itself against, which is what makes a disagreement a bug
rather than a difference of reading. No hardware needed; it runs in CI.
