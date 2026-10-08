# `victim_raw` — the reference responder with no TCP/IP stack

*🤖WARNING🤖: This file LLM generated and may read oddly. Will eventually be human-edited
for that real-life touch and typos.*

```bash
tools/fetch_sdk.sh f429
make -C firmware BOARD=f429 APP=victim-raw
#  -> Build/f429-victim-raw/ethtimer_victim_raw.bin
```

Same wire format as [`../victim`](../victim) — same magic, same fields, same
offsets, the same `--target jitter` on the host — so the two are measured by
identical host code and the difference between them is the firmware and nothing
else. That comparison is the reason this exists.

## What it buys

Two NUCLEO-F429ZI boards, same cable, same instrument, 20 000 exchanges of
64-byte frames, back to back:

| | lwIP responder | **this one** | |
|---|---|---|---|
| responder's own interval, median | 164.961 µs | **1.972 µs** | 84× |
| responder's own interval, sd | 62.350 µs | **0.023 µs** | 2 700× |
| **path median** | 21.506 µs | **14.633 µs** | **−6.87 µs** |
| **path sd** | 0.060 µs | **0.052 µs** | 1.2× |
| exchanges/s | 1 156 | **1 520** | 1.3× |

The responder's own interval is **1.972 µs at every percentile from p0.1 to
p99** — a flat line, not a distribution. Its 23 ns of standard deviation is all
in the top 1 %.

**The row that matters is the path median, and it is not obvious why.** The lwIP
responder already reported its own 165 µs and the host already subtracted it, so
why did removing lwIP move the path figure by 6.87 µs?

Because an interval a responder can put in its own reply has to END before the
frame leaves. Everything from "hand it to the driver" to "first bit on the wire"
is outside it and lands in the path number as a constant: with lwIP that tail
was a pbuf allocation, a copy, a route lookup and `HAL_ETH_Transmit_IT`'s
descriptor bookkeeping. 6.87 µs of it. Self-reporting removes what it can
measure; it cannot remove what happens after it stops measuring.

So removing lwIP did not mainly make the responder faster — it made the path
number **more nearly the path**. The remaining 14.633 µs is two frame
serialisations, two MAC transmit paths and the instrument's own interrupt
latency, and on a direct cable it is nothing else.

Two runs agree on that median to **+0.000 µs** and on its spread to +0.000 µs,
so `analyse.py --vs` resolves a device that adds a few tens of nanoseconds.

## How it works

No stack. The reply frame — Ethernet, IPv4 and UDP headers and the payload's
constant fields — is built once at start-up into a static buffer, and a transmit
descriptor points at it permanently.

The Ethernet receive interrupt then:

1. has the cycle counter already latched by the board's ISR, before any of this
   code exists;
2. reads about twenty bytes of the incoming frame to decide it is ours;
3. stores four words into the reply (tag, sequence, `rx_cyc`, `tx_cyc`);
4. sets one OWN bit and pokes `DMATPDR`.

No allocation, no copy, no stack, and nothing between the frame arriving and the
frame leaving that can take a different number of cycles on different exchanges.

**The IPv4 header checksum is computed by the MAC.**
`ETH_DMATXDESC_CIC_IPV4HEADER` on the transmit descriptor makes the DMA insert
it, so the reply length can mirror the request's — which is the frame-size
sweep — with the hot path computing nothing. The UDP checksum is left at zero,
which IPv4 permits.

ARP requests are answered too, from the same interrupt, out of a second buffer.
They happen once per capture, and keeping one place in charge of the transmit
ring is worth more than keeping them off the interrupt.

## F429 only, and that is not laziness

The whole of `rawnet.c` is the F4 Ethernet DMA's descriptor format and its
`DMASR`/`DMATPDR` registers. The H7's DMA is a different generation with a
different descriptor layout and a per-channel status register; the F7's is the
F4's. A board-generic version of this file would be a board-generic version of
the thing it exists to avoid.

[`../victim`](../victim) is the portable responder and builds for every board.
Use it unless the path figure's last few microseconds matter.

## What it does not handle

Deliberately, and each one is a branch not taken: IP fragmentation, IP options,
VLAN tags, ICMP (so it does not answer `ping`), DHCP, TCP, IPv6, and more than
one peer at a time. Anything that is not an ARP request for its address or a UDP
datagram to its port is counted and dropped.

Its address is a build-time constant, like the lwIP responder's, because a
control channel is something that could run while an exchange is being measured:

```bash
make -C firmware BOARD=f429 APP=victim-raw EXTRA_DEFS='-DETV_IP2=0 -DETV_IP3=77'
```

Its MAC is `02:00:00:00:00:02`, one byte different from the lwIP responder's, so
both can sit on one segment while they are being compared.

## Three traps, for the next person doing this

**The Ethernet frame length is not the payload length.** The first version
worked back from the descriptor's frame length and replied 60 bytes to a 64-byte
request — two reasons, both invisible from the descriptor: the MAC strips the
4-byte CRC from type frames (`MACCR`'s `CSTF`), so subtracting 4 subtracts it
twice; and a frame shorter than 60 bytes arrives padded, so the Ethernet length
over-reports a short datagram. The reply length now comes from the **UDP
header's own length field**, which is authoritative and independent of both.

**The cycle counter stops, and this application does not call
`board_link_tick()`.** That is where the instrument's counter heal lives, so
this one reported an interval of exactly zero for an afternoon — not an error,
an endpoint that appears to contribute nothing. The heal is now
`board_dwt_tick()`, its own function for exactly this reason, and this
application's loop calls it. See `dwt_enable()` in the board's `board.c`.

**A silent responder has six causes and they are six repairs.** A cable carrying
something else, an IP header it does not parse, a port mismatch, a stale
protocol version, a frame too short, or a receive ring that has lost step with
the DMA. It counts them separately and prints them on the console when one
moves — at most once a second, and only on movement, because a UART transmit is
tens of microseconds and a line on a timer would appear as a regular outlier in
somebody's jitter histogram:

```
victim: frames=20003 seen=20000 sent=20000 arp=1 drop=2 (short=0 notip=2 notudp=0 port=0 magic=0)
```

## It also leaks a password on purpose

Both responders implement a password check whose running time depends on how many
leading bytes of a guess were right, and a constant-time one beside it. See
[`../common/pwcheck.h`](../common/pwcheck.h) and
[`../../demos/password/`](../../demos/password) — the recovery works on this
responder, and on the lwIP one the leak is real but buried under hundreds of
nanoseconds of its own variation.

## Sharing the board support

It compiles the board's `board.c` and calls `board_clock_console_init()` — the
half of `board_init()` that does not need a TCP/IP stack — so it runs the
**same clock tree as the instrument** rather than a copy of it. The lwIP half of
that file is compiled and then dropped by `--gc-sections`, because nothing here
references it; `firmware/Makefile` does not link lwIP for this application at
all, and the binary contains zero lwIP symbols (11 992 bytes of text against the
lwIP responder's 53 140).

The response path gets into the interrupt through `board_eth_isr_hook()`, a weak
no-op in the board's interrupt file that this application overrides. The
instrument does not override it and gets exactly the HAL path it had before the
hook existed.
