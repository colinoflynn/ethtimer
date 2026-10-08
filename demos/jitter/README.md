# `jitter` — measuring the path, not the endpoints

*🤖WARNING🤖: This file LLM generated and may read oddly. Will eventually be human-edited
for that real-life touch and typos.*

Two boards, a cable, and whatever you put between them. One board runs the
instrument; the other runs [`firmware/victim`](../../firmware/victim), a
reference responder that answers a UDP request and **reports its own
receive-to-send interval in every reply**.

So a capture holds three series, not one:

| | |
|---|---|
| **round trip** | `dt_hw`, in the instrument's cycles: its transmit-complete interrupt to its receive interrupt |
| **responder** | `tx_cyc - rx_cyc`, in the responder's cycles: its receive interrupt to the moment it handed the reply on |
| **path** | the difference — everything outside both endpoints' software |

Measured on a direct 100 Mbit full-duplex link between two NUCLEO-F429ZI
boards, 20 000 exchanges of 64-byte frames each way:

```
                    n      p1       p50       p99     p99.9       max        sd
round trip      20000 185.767   186.600   329.728   337.167   484.361    62.199
responder       20000 164.411   165.200   308.400   315.683   463.022    62.235
path            20000  21.233    21.378    21.550    21.778    21.922     0.082
```

**The responder is slow and it jitters, and it does not matter, because it says
so.** It accounts for 89 % of the round trip and all of its variance. Taking its
own interval off leaves a path figure with **82 nanoseconds** of standard
deviation -- a seven-hundredth of the round trip's. A responder built to be fast
and then *assumed* constant would have put all 62 us of that into somebody's
conclusion about a switch.

And it repeats. Two independent 20 000-exchange runs over the same cable, back
to back:

```
                   baseline   under test        delta
  path median        21.378       21.378       +0.000
  path p99           21.550       21.550       -0.000
  path sd             0.082        0.082       -0.000
```

So `--vs` can resolve a device that adds a hundred nanoseconds.

## Two responders, and which to use

There are two, with the same wire format, so the same `--target jitter` measures
either and the difference between them is the firmware and nothing else:

| | [`victim`](../../firmware/victim) (lwIP) | [`victim_raw`](../../firmware/victim_raw) (bare metal) |
|---|---|---|
| boards | every board | **f429 only** |
| builds with | `APP=victim` | `APP=victim-raw` |
| its own interval, median | 164.961 us | **1.972 us** |
| its own interval, sd | 62.350 us | **0.023 us** |
| **path median** | 21.506 us | **14.633 us** |
| **path sd** | 0.060 us | **0.052 us** |
| exchanges/s | 1 156 | **1 520** |

Same two boards, same cable, same instrument, 20 000 exchanges of 64-byte frames,
back to back.

**Why the path median moved by 6.87 us is the interesting part**, because the
lwIP responder already reported its 165 us and the host already subtracted it.
An interval a responder can put in its own reply has to END before the frame
leaves, so everything from "hand it to the driver" to "first bit on the wire" is
outside it and lands in the path number as a constant. With lwIP that tail was a
pbuf allocation, a copy, a route lookup and the HAL's descriptor bookkeeping --
6.87 us of it. **Self-reporting removes what it can measure; it cannot remove
what happens after it stops measuring.** Taking lwIP out removed it.

So: use `victim-raw` on an F429 when the path number's last few microseconds
matter, and `victim` everywhere else. The portable one is still a good
instrument -- its *spread* is 0.060 us against the other's 0.052 -- it just
carries a larger constant it cannot account for.

That is the whole design argument, and the firmware's own comments make it at
more length: a responder that is slow but honest beats one that is fast and
assumed.

## Run it

Flash one board with the responder and the other with the instrument:

```bash
tools/fetch_sdk.sh f429
make -C firmware BOARD=f429 APP=victim-raw  # the bare-metal responder, f429 only
make -C firmware BOARD=f429                 # the instrument
```

`APP=victim` builds the portable responder instead; see *Two responders* below
for which to use.

The responder comes up at **192.168.7.10** and the instrument defaults to
**192.168.7.20**, so a direct cable needs no arguments beyond the port. Its
console prints what it is and then stays quiet:

```
NUCLEO-F429ZI ethtimer reference responder v1 (bare metal, no lwIP)
  UDP port 7777, cycle counter 180 MHz
  address 192.168.7.10
  the reply is built in the receive interrupt; this loop only watches the link
victim: link 100F
```

Then:

```bash
python -m ethtimer.cli --target jitter --n 20000 --out direct.npz
python demos/jitter/analyse.py direct.npz --hist 20
```

It exits **2**: there is no key, so `campaign` reports NOT CHECKED. See
[`../dns/README.md`](../dns/README.md) — same point, same reason. What takes the
acceptance check's place here is narrower and real, and in the next section.

A different subnet needs the responder rebuilt, because it has no control link
at all — by design, since a command channel is something that could run while an
exchange is being measured:

```bash
make -C firmware BOARD=f429 APP=victim EXTRA_DEFS='-DETV_IP2=0 -DETV_IP3=77'
```

## Measuring a device

Put the thing under test between the boards and difference the two runs:

```bash
python -m ethtimer.cli --target jitter --n 50000 --out direct.npz     # cable only
#  ... insert the switch ...
python -m ethtimer.cli --target jitter --n 50000 --out switch.npz
python demos/jitter/analyse.py direct.npz --vs switch.npz
```

`--vs` prints the difference and nothing else, because **that is the only form
in which "this switch adds N microseconds" is a sentence this tool can
support.**

### What `path` is not

It is not any device's latency. It is that device's latency **plus** two frame
serialisations, two MAC transmit paths and the instrument's own interrupt
latency. On a direct cable those constants are the entire 21.4 µs above — there
is nothing else they could be.

So:

* the **spread** of `path` — `sd`, `p99 - p50`, the tail — is the path's, and it
  is what this demo is for;
* the **median** of `path` is a latency plus a constant, and `--vs` is the only
  honest use of it.

Keep the frame size the same across runs you intend to difference: serialisation
is 80 ns per byte each way at 100 Mbit, and a store-and-forward hop charges for
the frame twice. `analyse.py --vs` warns if the two captures disagree about it.

## What is checked, with no key

Three things, none of which is a record count.

* **Every record carries the responder's magic and version.** A window that
  drifted four bytes fails here instead of producing a jitter histogram of
  something else. This is [`../dns`](../dns)'s question-section check in another
  form.
* **The responder's own counter must advance by exactly one per record.** It
  owns that counter, so a gap is a reply that was sent and not seen, and a step
  backwards is **reordering** — which the instrument's timeout count cannot see
  at all, because from its side a reordered reply is just the next datagram.
  Through a loaded switch, reordering happens.
* **The responder's reported interval must be greater than zero**, checked on
  the probe exchange before a capture is spent. A dead cycle counter reports
  zero, zero is not an error anywhere downstream, and the path figure would
  then silently be the whole round trip — about nine times the truth.

## Validating a build

The responder is the one target here with no real device's scheduling in it, so
a direct-cable capture against it is a measurement of **the instrument** and
almost nothing else. Keep one and compare after a new board, a new toolchain or
a bumped SDK pin:

```bash
python demos/jitter/analyse.py known_good.npz --vs after_change.npz
```

A `path` median that moved by more than a few tens of nanoseconds is a real
change in the instrument. Nothing else in `demos/` isolates it that way.

## Two things this demo found about itself

Both are in the firmware's comments at length; both produced a plausible number
rather than an error, which is why they are worth reading before trusting a
distribution from any similar rig.

**1. The path figure was bimodal, and it was the responder.** The first 20 000
exchanges came out as 14 940 records at 21.1 µs and 5 000 at 27.6 µs — a 25 %
population exactly 6.5 µs slower, which is one frame time at 100 Mbit. One in
four is `ETH_TX_DESC_CNT`: ST's NO_SYS lwIP port only sweeps completed transmit
descriptors when a send finds the ring full, so one exchange in four paid for
the sweep. The responder now releases them at the top of its handler, every
time — which both makes the cost predictable and moves it *inside* the interval
it reports, so it is subtracted. Path jitter went from 2.816 µs to 0.063 µs.

Paying a cost every exchange to stop paying it unpredictably every fourth is the
right trade for a reference.

**2. The cycle counter stops, and a stopped counter reports zero.** The DWT
lives in the debug power domain. Enabling it at start-up works and then the
counter **freezes** -- measured here, it accumulated 484 ms of uptime and held
that value, so the Ethernet interrupt latched a constant, `tx_cyc - rx_cyc` came
out as exactly 0, and `path` became the whole round trip. Intermittent: it
survived one debugger reset and not the next, which is the worst kind.

**And `DWT->CTRL` read `0x40000001` throughout -- CYCCNTENA set -- while the
counter was not counting.** The first attempt at self-healing tested that bit
and therefore never fired, which is the same mistake as trusting a status
register over an observation.

What fixed it: `board_init()` enables the counter *after* the clock tree and the
console rather than before, and **proves** it by watching it move, retrying if
it has not; `board_link_tick()` re-enables it if it has not advanced in 100 ms,
which is millions of cycles at any clock this part runs and never inside a
measured exchange. Measured 5 of 5 live over flash-reset-wait cycles that had
previously been about half. On top of that the responder says so on its console,
and this demo's `prepare()` refuses to start a capture whose probe reports zero.
Four places, because exactly zero is not a number anyone looks at twice.

## Files

| | |
|---|---|
| [`jitter_target.py`](jitter_target.py) | the adapter: one held request, the whole reply header kept |
| [`etv.py`](etv.py) | the responder's wire format, mirrored from the firmware header |
| [`analyse.py`](analyse.py) | the decomposition, percentiles, a histogram, and `--vs` |
| [`../../firmware/victim/`](../../firmware/victim) | the responder itself, built from the same board support as the instrument |

[`../../tests/test_victim_proto.py`](../../tests/test_victim_proto.py) holds
`etv.py` against the firmware header — the constants, and that the reply field
table tiles the header with no overlap and no gap. An offset that drifts by four
bytes swaps `tx_cyc` for `clk_hz` and the path figure comes out plausible.

## Be reasonable about it

1 150 exchanges a second is a small load, but it is a load. Point it at your own
equipment, and use `--gap` to slow it down.
