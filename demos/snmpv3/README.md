# `snmpv3` — timing an authPriv GET

*🤖WARNING🤖: This file LLM generated and may read oddly. Will eventually be human-edited
for that real-life touch and typos.*

Sends the same SNMPv3 authPriv `GET sysDescr.0` over and over, times each
turnaround, and keeps two blocks of the encrypted scopedPDU out of each reply.

Then it checks itself: `E(K, C1)` must equal `P2 ^ C2` on every block sampled,
under the agent's real privacy key. If it does not, the capture is rejected
rather than saved.

## What the other end has to be

An SNMPv3 agent with:

* **authPriv** security level, **AES-128** privacy (RFC 3826) and HMAC-SHA-1
  authentication — or MD5, see `snmpv3-harmony` below;
* credentials you know. The adapter is the client, and an agent does not encrypt
  a reply to anyone it has not authenticated, so there is no exchange to time
  without them;
* `sysDescr.0` readable, and a **constant** answer. The acceptance check needs
  the response plaintext to be the same bytes every time.

The reference device is a NUCLEO-F429ZI running lwIP's stock SNMPv3 agent with
mbedTLS underneath. Credentials are at the top of
[`snmpv3_target.py`](snmpv3_target.py); change them there, or subclass as
[`snmpv3_harmony_target.py`](snmpv3_harmony_target.py) does.

## Run it

```bash
python -m ethtimer.cli --target snmpv3 --n 20000 \
    --ip 192.168.7.20 --gw 192.168.7.1 --victim 192.168.7.10 \
    --out snmpv3.npz
```

```
snmpv3: 20000 records of 20000 requested, 0 timeouts, 0 short, 0 bad dt,
        0 dropped, 497/s, median 1166.5 us
  E(K, aes_in) == aes_out on 256/256 blocks (256 exchanges x 1, spread across
  the capture)
```

`--spot-check 0` checks every block instead of 256 spread through the capture.

## Why one held request is enough here

`fixed` mode: the board holds a single request datagram and repeats it for a
whole batch with the host out of the loop, which is the fastest path the
instrument has.

That works because **the agent supplies the variation**. RFC 3826's CFB IV is
`engineBoots || engineTime || privParam`, and `engineTime` ticks, so the same
request produces a different IV — and therefore a different first ciphertext
block `C1` — on every exchange. The ratio of exchanges to distinct AES inputs is
a property of the agent's clock, not of this adapter: one reference agent gives
1:1, another whose IV is derived from a coarser clock gives about 5:1.

## The two traps this adapter exists to document

Both were found the expensive way, and both produce a capture that looks
perfect.

**1. The window moves *during* a capture.** The reply carries `engineTime` as a
BER INTEGER ahead of the ciphertext. When the agent's uptime crosses 127
seconds, that integer needs a second byte and everything after it shifts by one.
A 20 000-exchange capture verified for its first 5 956 records and failed every
one after — with `n_short = 0`, `n_timeout = 0` and perfectly ordinary timings,
because one-byte-shifted ciphertext still arrives on schedule.

Re-reading the offset between batches (`Target.between_batches`) is **not
enough**: it only catches the move at a batch boundary. Measured, the offset
moved at record 11 802 and the batch ran to 11 912, leaving 110 records stale;
on a confirming run the boundary fell earlier and 1 716 were stale.

What closes it is a window that **identifies its own alignment**. The adapter
keeps the BER OCTET STRING header that introduces the ciphertext — public,
key-free — and locates the blocks relative to that marker in *each record*. A
capture started so the boundary falls inside it then verifies 20 000 / 20 000,
and the saved `align` array shows which records moved.

*An offset is an assumption about a whole capture. A landmark is a check on
every record.*

**2. The agent can stop answering the way it started.** RFC 3414 bounds
`msgAuthoritativeEngineTime` to ±150 s of the agent's clock. A request built
once at the start of a long capture ages out, and the agent then answers with a
well-formed **unencrypted usmStats report** — right size, on time, no
ciphertext. A 250 000-exchange run returned 75 534 usable records and 174 466
reports, with `n_timeout = 0`, `n_short = 0` and a normal median. The acceptance
check passed, because it ran on the survivors of the filter that had already
removed the rest.

Two things stop it: `campaign` raises when more than a couple of percent of
exchanges produce no usable block, and this adapter re-stamps its request every
batch. `msgID` may vary; `request_id` may **not**, because it sits inside the
encrypted scopedPDU and changing it changes the known plaintext.

## The pair

```
aes_in  = C1                     the first ciphertext block, in clear
aes_out = ks2 = P2 ^ C2          because keystream block n is E(K, C_(n-1))
```

`P2` is standard MIB-2 BER and byte-identical on every response, so the second
keystream block is recoverable from the wire alone — which is what makes the
pair, and therefore the check, possible without the key.

## `snmpv3-harmony`

The same protocol work against an agent that localizes with **MD5** instead of
SHA-1, on a different subnet and with different credentials. It is a subclass
rather than a second adapter because that is the entire difference.

It has **not** been run through `campaign` — see the note at the top of
[`snmpv3_harmony_target.py`](snmpv3_harmony_target.py). Treat it as untested
until it has.

## Files

| | |
|---|---|
| [`snmpv3_target.py`](snmpv3_target.py) | the adapter |
| [`snmpv3_harmony_target.py`](snmpv3_harmony_target.py) | the MD5-localized variant |
| [`snmp3.py`](snmp3.py) | SNMPv3 USM: build an authPriv GET, parse the reply, reproduce RFC 3414 key localization. Deliberately small, and not a general SNMP library |
