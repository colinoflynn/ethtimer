# `dns` — the demo with nothing to set up

Measures how long a resolver takes to answer a query, over 20 000 queries,
counted in the instrument's own clock cycles.

**Run this one first.** Every other demo needs a device built and flashed with
known credentials before it can say anything; this one needs a DNS server, and
your router is one. So it is what tells you the build, the link, the serial
framing, the window, the batching and the record layout all work — before you
start wondering whether an adapter is at fault.

## Run it

Give the instrument an address on your LAN and point it at your resolver:

```bash
python -m ethtimer.cli --target dns --n 20000 \
    --ip 192.168.1.50 --mask 255.255.255.0 --gw 192.168.1.1 \
    --victim 192.168.1.1 \
    --out dns.npz
```

`--ip` must be free on that subnet; the instrument does not do DHCP, because a
lease renewal in the middle of a capture is a gap in the data. `--victim` is the
resolver. Expect something like:

```
NUCLEO-F429ZI fw=2 proto=2 clk=180000000 Hz link=100F max_req=512 max_win=320
              ring=98304 B bank=32768 B/1024
dns: example.com -> rcode 0, 1 answer(s), 45 B reply
reply 45 B, keeping [12:29] (17 B)
dns: question section identical on all 20000 records
  *** NOT CHECKED: this target has no truth key ... ***
dns: 20000 records of 20000 requested, 0 timeouts, 0 short, 0 bad dt,
     0 dropped, 485/s, median 213.161 us, sd 1783.335 us
```

**It exits 2, and that is the correct answer.** There is no key in DNS, so
`campaign` cannot say the captured bytes are the ones that device produced. Exit
2 means "captured, not verified"; the other demos exit 0. If you want a demo
whose success is a *statement about the data*, that is what the other three are
for — and the gap between those two outcomes is the whole reason this repository
has an acceptance check at all.

## What it does check, without a key

The window is the reply's **question section**, which a resolver echoes from the
query byte for byte. So every record must hold the same 17-ish bytes, and
`blocks()` raises if any of them does not. That is a real check on the window
offset, the record stride and the framing — it would catch a window landing four
bytes off, or a record layout the host decoded wrongly — and it needs no secret.

The **answer** section is deliberately excluded. A cached A record's TTL counts
down between exchanges, so an answer section that differs between records is a
*correct* capture of a changing reply, and from the bytes alone that is
indistinguishable from a window that has drifted. Keeping only the echo makes
"the bytes are not constant" mean exactly one thing.

## What the number means

A cached answer from a consumer router is a few hundred microseconds of that
router's own turnaround, and the standard deviation will be large because the
box is doing other things. The measurement is sound; the *subject* is just
noisy. Nothing here says anything about cryptography — the resolver is not doing
any.

If you want a tight distribution to look at, point it at something that is not
busy. A minimal responder on the host at the other end of the cable gives a
median of about 200 µs with the F429 as the instrument, and that is mostly the
host's own network stack.

## Why `bank` and not `fixed`

A resolver may well collapse a repeated query — same transaction ID, same
question, back to back — into one response, or rate-limit it. So each exchange
gets a fresh transaction ID out of the request bank. That also means this demo
exercises the bank upload path that the `oscore` and `tls` adapters depend on,
which is worth having in the demo you run first.

`--n 20000` against a LAN resolver is a few dozen bank uploads of 1 024 entries
each on an F429.

## Options

`DnsTarget(qname=...)` asks about a different name. Anything a resolver answers
will do — including a name that does not resolve, since an NXDOMAIN is still a
timed exchange with a question section in it. The adapter sends no EDNS record
on purpose: an OPT pseudo-section in the reply would be one more varying field
between the window and the end of the datagram.

## Be reasonable about it

20 000 queries a second at somebody else's recursive resolver is a load test
they did not agree to. Point this at your own equipment. `--gap` sets the idle
time between exchanges if you want to slow it down.
