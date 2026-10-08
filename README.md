# ethtimer

**A general-purpose instrument for timing Ethernet request/response exchanges,
to the cycle.**

A NUCLEO board sends host-supplied bytes — as a UDP datagram, or on a held-open
TCP connection — latches `DWT->CYCCNT` in the Ethernet interrupts around the
exchange, keeps a chosen slice of the reply, and streams the records back over a
binary serial link. Firmware and host library are **one project**, so the wire
protocol is defined once and a test holds the two halves together.

What it measures is the **turnaround of the device at the other end of the
cable**: from the transmit-complete interrupt for the request to the receive
interrupt for the reply, counted in the instrument's own clock cycles. No host
scheduler, no USB stack and no operating system sit inside that interval.

**Nothing in it knows about any application protocol.** The host says "send
these bytes, keep those bytes of what comes back"; what the bytes mean is the
host's business. Pointing it at a new protocol is a host-side adapter of a
hundred lines or so — not a firmware change, and not a rebuild.

```bash
git clone https://github.com/colinoflynn/ethtimer && cd ethtimer
pip install -e .                      # or: pip install numpy pyserial
python -m pytest -q                   # host-side tests, no hardware needed

tools/fetch_sdk.sh f429                # pull the pinned ST sources
make -C firmware BOARD=f429            # -> firmware/Build/f429/ethtimer.bin
# flash it (see "Flashing", below), then:

python -m ethtimer.cli --ports
python -m ethtimer.cli --target dns --n 20000 \
    --ip 192.168.1.50 --gw 192.168.1.1 --victim 192.168.1.1
```

The `dns` demo is the one to run first: the thing it measures is a resolver, and
you already have one. See [`demos/dns/README.md`](demos/dns/README.md).

With a second board you get [`demos/jitter`](demos/jitter), which is the one to
run when the question is about the **network** rather than about a device:

```bash
make -C firmware BOARD=f429 APP=victim     # the reference responder
# flash it to the other board, cable them together, then:
python -m ethtimer.cli --target jitter --n 20000 --out direct.npz
python demos/jitter/analyse.py direct.npz
```

## Contents

| | |
|---|---|
| [`firmware/`](firmware) | **two applications, one board support.** `src/` is the instrument: `inc/ethtimer.h` is the protocol defined once, `src/ethtimer.c` is framing, dispatch and the capture loop, `src/main.c` is about twenty lines. [`victim/`](firmware/victim) is a reference responder that reports its own turnaround (`make APP=victim`). `boards/<b>/` is one board's clock tree, console, PHY, startup and linker script, shared by both |
| [`ethtimer/`](ethtimer) | the host library. `proto.py`/`device.py` speak the wire protocol and know UDP, TCP and "bytes"; `target.py` is what an adapter must supply; `campaign.py` is everything that is the same for every protocol; `cli.py` is the command line; `aes.py` is the reference AES the acceptance check uses |
| [`demos/`](demos) | protocol adapters: `dns`, `snmpv3`, `oscore`, `tls`. Each is a `Target` plus the protocol library it needs, and none of them is in the instrument's import path |
| [`tests/`](tests) | **no hardware, no toolchain.** The protocol mirror test, the AES vectors, the OSCORE RFC vectors |
| [`examples/`](examples) | worked captures that do something `cli.py` does not — chunked output, for a run measured in hours |
| [`tools/`](tools) | `fetch_sdk.sh`, which pulls ST's sources at pinned tags |

## Boards

| board | build | core | record ring | request bank | state |
|---|---|---|---|---|---|
| NUCLEO-F746ZG | `make BOARD=f746` (default) | M7, 216 MHz | 128 KB | 64 KB / 2 048 | run as the instrument |
| NUCLEO-F429ZI | `make BOARD=f429` | M4, 180 MHz | 96 KB | 32 KB / 1 024 | run as the instrument |
| NUCLEO-H723ZG | `make BOARD=h723` | M7, 400 MHz | 128 KB | 64 KB / 2 048 | brought up; **no capture yet** — [why](firmware/boards/h723/README.md) |

All three carry a LAN8742 PHY and put the console on USART3, and nothing in
`firmware/src/` mentions a part number. Adding a board is a `boards/<name>/`
directory, a `board.mk`, and a line in `tools/fetch_sdk.sh` saying which ST
components that family needs — see
[`firmware/boards/README.md`](firmware/boards/README.md), which also lists the
five things that went wrong porting the H723, every one of which produced a
working-looking board rather than an error.

`make list` prints what this tree knows about.

The ring and the bank bound **different** things, which is why they are reported
separately in `GET_INFO` rather than assumed by the host:

* the **ring** is how many exchanges *one batch* holds. The host chunks a
  capture across batches regardless, so this bounds latency, not length — and
  `run()` refuses a batch longer than the ring rather than letting the device
  shorten it silently.
* the **bank** is how many *distinct* requests a replay-protected device can be
  given before the host has to stop and upload more.

A host therefore sizes its uploads from the board in front of it, not from a
constant it was compiled with.

### Two applications

`APP=victim` builds a **reference responder** from the same board support: it
answers a UDP request and reports its own receive-to-send interval in every
reply, so a host can take its contribution off and be left with the path's.
That is what [`demos/jitter`](demos/jitter) is for, and it is also the only
target here against which a capture measures *the instrument* and nothing else
— which makes it the thing to compare against after a new board, a new
toolchain or a bumped SDK pin.

```bash
make -C firmware BOARD=f429 APP=victim   # -> Build/f429-victim/ethtimer_victim.bin
```

Sharing the board support is deliberate: a responder built on its own clock
tree and lwIP port would be a second thing that could be wrong, and the first
question about any jitter number is which end of the cable it came from.

### Flashing

The binary is a raw `.bin` for `0x08000000`. Any of these works:

```bash
# ST-LINK mass storage: copy to the board's volume.
cp firmware/Build/f429/ethtimer.bin /e/

# or over SWD
pyocd flash -t stm32f429xi --format bin --base-address 0x08000000 \
    firmware/Build/f429/ethtimer.bin
st-flash write firmware/Build/f429/ethtimer.bin 0x8000000
```

**Check it took.** A drag-and-drop copy can silently do nothing — no error, no
`FAIL.TXT`, and the board keeps running the old image. The confirmation is
`Device.info()` reporting the version and the limits you just built, not the
copy returning:

```python
from ethtimer import Device
with Device("COM9") as d:
    print(d.info())
# NUCLEO-F429ZI fw=2 proto=2 clk=180000000 Hz link=100F max_req=512 max_win=320
#               ring=98304 B bank=32768 B/1024
```

If more than one board is attached, resolve it by its probe serial — a drive
letter and a COM port both move on replug, and two boards of the same type share
a volume label. `python -m ethtimer.cli --ports` prints the serials.

## Using it

```python
from ethtimer import Device

with Device("COM9") as d:
    print(d.info())
    d.set_net(ip="192.168.7.20", mask="255.255.255.0",
              gw="192.168.7.1", victim="192.168.7.10")
    d.set_target(port=161, timeout_ms=200)
    reply = d.oneshot(request)          # warms ARP AND gives the reply length
    d.set_request(request)
    d.set_window(off=92, length=32)
    batch = d.capture(500_000, gap_us=200)
    print(batch.summary())
```

Addressing is **runtime state on the device**, so one binary drives a device on
any subnet; every setter is verified against a `GET_CONFIG` read-back and
`run()` refuses to start if the device does not hold what the caller asked for.

Or let `campaign` do the whole thing, which is what the CLI does:

```bash
python -m ethtimer.cli --target snmpv3 --n 20000 --out run.npz
python -m ethtimer.cli --module path/to/my_target.py --n 5000
```

`--target` names an adapter in `demos/`; `--module` loads one from a file, which
is what a new protocol wants before it is worth a directory. Either way the
capture loop, the diagnostics, the acceptance check and the saved layout are the
same. Exit status: **0** verified, **2** captured but there was no key to verify
it against, non-zero-and-noisy for anything that raised.

## The demos

Each is an adapter plus the protocol library it needs. None of them is a
dependency of the instrument, and none of them can reach into it: the import
edge runs one way only.

| demo | transport | mode | what it needs at the other end |
|---|---|---|---|
| [`dns`](demos/dns) | UDP 53 | `bank` | **any resolver.** The zero-setup demo |
| [`jitter`](demos/jitter) | UDP 7777 | `fixed` | a second board running `firmware/victim`. Measures the **path**, not an endpoint |
| [`snmpv3`](demos/snmpv3) | UDP 161 | `fixed` | an SNMPv3 authPriv agent with known credentials |
| [`oscore`](demos/oscore) | UDP 5683 | `bank` | an OSCORE server with a known master secret |
| [`tls`](demos/tls) | TCP 443 | `relay` + `bank` | an HTTPS server speaking TLS 1.2 CBC |

### Where the requests come from

The one thing that differs between protocols, once the timing is shared, is
whether a device will answer the same request twice. A target declares which of
these it needs and `campaign` does the rest.

| mode | the device | mechanism |
|---|---|---|
| `fixed` | supplies the varying input itself | one request held on the board, repeated. SNMPv3: `engineTime` ticks, so the CFB IV — and therefore `C1` — differs every exchange |
| `bank` | refuses a repeat | the host uploads a run of **distinct** requests and the board plays it **once**. OSCORE's replay window; TLS's record sequence numbers; a DNS transaction ID |
| `relay` | — | one exchange per host round trip, the adapter driving. For a handshake, where a record depends on the one before it and nothing can be pre-generated |

**A bank is played once and never cycled.** Re-sending a sequence number the
device has retired is answered with a reject rather than the reply this
measures, and that arrives as a batch of rejects minutes into a capture rather
than at the first exchange.

TLS uses `relay` for the handshake and `bank` for the bulk phase. A serial round
trip per exchange costs an order of magnitude in rate and buys nothing there,
because the host holds the TLS write state and can generate records ahead of the
responses.

## Acceptance: a capture checks its own data

**Every capture checks the data, not the plumbing**, and the check is the same
sentence for every protocol: encrypt the captured **input** under the device's
real key and compare against the captured **output**. One `E(K, x) == y` per
block.

That works because each of these protocols puts both ends of an AES call on the
wire:

| protocol | AES input | AES output |
|---|---|---|
| SNMPv3 (CFB) | `C1`, the first ciphertext block, in clear | `ks2 = P2 ^ C2` |
| OSCORE (CCM/CTR) | `A_1`, the counter block, from the public Partial IV | `P ^ C` |
| TLS 1.2 (CBC) | `P_i ^ C_(i-1)`, the explicit IV in clear | `C_i` |

A record count proves none of that. This one statement exercises the request,
the reply parse, the window offset and the transfer at once, and it is the
difference between "the instrument moved bytes" and "the bytes are the ones that
device produced". `campaign.run()` raises if it fails, so a capture that returns
has verified. The real key is used **only** here; no adapter may consult it when
producing the pairs.

The default sample is spread **across** the capture rather than taken from the
front: a banked capture is uploaded in chunks, and the mistake a sample is most
likely to meet is an exchange-to-request mapping that is right in the first bank
and wrong in every later one. `--spot-check 0` checks every block.

`dns` has no key, so it is **not** checked, and `campaign` says so in those
words rather than reporting a record count and letting it read as a pass. That
asymmetry is the point of the section.

Measured on the reference devices:

```
snmpv3  20000 exchanges, 0 timeouts, 0 short, 0 bad dt, 497/s, median 1166.5 us
        E(K, C1) == ks2 on every block checked
oscore  20000 exchanges, 0 timeouts, 0 short, 0 bad dt, 510/s, median  937.4 us
        E(K, A_1) == keystream on ALL 20000 pairs, 20000 distinct AES inputs
tls      5000 exchanges, 0 timeouts, 0 short,            156/s, median  799.5 us
        E(K, P_i ^ C_(i-1)) == C_i on ALL 75000 pairs (15 per exchange)
dns      5000 exchanges, 0 timeouts, 0 short, 0 bad dt, 485/s, median  213.2 us
        question section identical on all 5000 records; NOT CHECKED (no key)
jitter  20000 exchanges, 0 timeouts, 0 short, 0 bad dt, 1156/s
        round trip median 186.600 us sd 62.199;  responder's own 165.200 sd 62.235
        PATH median  21.378 us sd  0.082  <- the responder's self-report took the
        other 62 us of variation off, and two back-to-back runs agree on that
        median to +0.000 us.  NOT CHECKED (no key)
```

The `dns` row was taken with an F429 as the instrument against a host-side
responder, and is the row you can reproduce without building anything else.

## The control protocol, and why it is binary

```
 0     'E'
 1     'T'
 2     type     u8
 3..4  len      u16
 5..   payload
 last2 crc16    u16   CCITT-FALSE over bytes [2 .. 5+len)
```

Commands: `PING`, `GET_INFO`, `GET_CONFIG`, `SET_NET`, `SET_TARGET`,
`SET_REQUEST`, `SET_WINDOW`, `ONESHOT`, `RUN`, `RESET`, `SET_BANK`,
`TCP_CONNECT`, `TCP_CLOSE`, `RELAY`. Responses: `ACK`, `ERR`, `INFO`, `CONFIG`,
`BATCH_HDR`, `BATCH_DATA`, `BATCH_END`, `ONESHOT`, `RELAY`.

`ET_PROTO_VERSION` is **2**. The two halves move together and `Device` checks
the version out of `GET_INFO` before using anything an older board lacks, so a
stale board is *named* rather than left to answer "bad argument" — which would
send the reader to look at their arguments.

Four properties, each of which is there because the alternative cost real time:

* **A length and a checksum.** The CRC-16 covers the type and the length as well
  as the payload, so a corrupted length cannot silently reframe the stream and a
  truncated reply can be told from a short one.
* **State is readable.** Sticky device state with no read-back is how a capture
  window left over from a previous run produces **zero records with zero
  timeouts** and reads exactly like a dead device.
* **No hex.** A 128-byte request is 128 bytes, not 256 characters.
* **One framing.** Text commands plus a binary record stream means the host
  switches parsing modes mid-conversation.

`tests/test_proto.py` holds the two definitions together **exhaustively**: every
`ET_CMD_*`, `ET_RSP_*`, `ET_ERR_*`, `ET_PROTO_*` and `ET_LINK_*` in the header
must have a mirror on the host and a row in the value table, or an explicit
entry in `NOT_MIRRORED` saying why not. A hand-written table was green on the
day nine constants were added to one side only.

**A record is `win_len + 12` bytes**: the window, then `dt_hw`, `dt_sw`, `seq`
as `u32`. Nothing is ever written *inside* the window. `BATCH_HDR` states
`rec_len`, `win_off`, `win_len` and the device clock, so the host parses what the
device produced rather than what it assumed.

## What a capture reports, and why each number exists

`BATCH_END` carries four counts, and they are not interchangeable:

* **`n_short`** — a reply arrived but was too short for the window. Counted, not
  skipped: skipping it returns a short batch with `timeouts=0`, which reads as a
  successful small capture.
* **`n_timeout`** — no reply inside the per-exchange timeout.
* **`n_bad_dt`** — the subtraction did not yield a duration. Roughly once in
  5 000 TCP exchanges the receive interrupt latches before the transmit-complete
  one and `dt_hw` wraps to about 2³². One such record took a capture's standard
  deviation from **6.8 µs to 281 163 µs** while leaving the median untouched.
* **`n_dropped`** — the exchange produced no usable block for the adapter.
  `campaign` **raises** above a couple of percent, because the survivors still
  verify: a 250 000-exchange capture once returned 75 534 usable records with
  `n_timeout = 0`, `n_short = 0`, a normal median and a passing acceptance
  check, the other 174 466 replies having been well-formed and unencrypted.

Link speed is **refused, not corrected**: `run()` rejects anything but 100 Mbit
full duplex. A 10 Mbit half-duplex link reads as up exactly like 100 Mbit full,
serialises a frame ten times more slowly, and erases the structure this measures.
`--no-link-check` exists for diagnosing a link, never for collecting.

## Writing an adapter

Subclass `ethtimer.target.Target`, implement `port`, `prepare`, `requests`,
`window` and `blocks`, and register it. [`demos/dns`](demos/dns) is the short
one to copy; `ethtimer/target.py` documents every hook and why it exists.

Two of those hooks earn their keep in ways that are not obvious:

* **`blocks(window, index)` is given `index`, and it is not `arange(n)`.** A
  timed-out or short exchange leaves no record but still consumes a request, so
  the k-th record is generally not the k-th request. An adapter whose input is
  derived from what it *sent* and that assumes otherwise pairs every record with
  a neighbouring input — which does not fail loudly, it just verifies nowhere.
* **`between_batches` is not enough on its own.** Some protocols move the bytes
  you are keeping *during* a capture: SNMPv3 carries `engineTime` ahead of the
  ciphertext as a BER integer, so when the device's uptime crosses 127 seconds
  the length grows by a byte and everything after it shifts. Re-reading the
  offset between batches catches it only at a batch boundary — measured, the
  offset moved at record 11 802 and the batch ran to 11 912, leaving 110 records
  stale, and on another run 1 716. What closes it is a window that **identifies
  its own alignment**: keep a public, key-free landmark and locate the blocks
  relative to it in *each* record. An offset is an assumption about a whole
  capture; a landmark is a check on every record.

## Not implemented

* **IPv6, and more than one device at a time.**
* **A device-side diagnostic ring**, for comparing the remote measurement
  against the measured device's own cycle counter. That needs a cooperating
  device answering on a second port.
* **Resuming a capture whose TLS session dropped.** One session carries the
  whole run by design, because the record key has to be the same throughout. If
  it drops, the capture stops. `campaign` writes each batch to a `.part` file as
  it lands, so what was collected survives; nothing here continues it.

## Requirements

* **Host:** Python 3.9+, `numpy`, `pyserial` — that is all the instrument
  needs, and `ethtimer/aes.py` is self-contained so that even the acceptance
  check adds nothing. Two **demos** need `cryptography`: `oscore` for AES-CCM,
  and `tls` for its certificate helper. `pip install -e ".[demos]"` adds it.
* **Firmware:** `arm-none-eabi-gcc` and GNU make. Built and tested with GCC 10.3
  and 13.x. `tools/fetch_sdk.sh` needs `git` and `bash`.
* **Hardware:** a NUCLEO-F429ZI or NUCLEO-F746ZG (or an H723ZG, once one has
  taken a capture), and a 100 Mbit full-duplex link to whatever you are
  measuring. A direct cable is the arrangement the demos describe.

## Licence

Apache-2.0 — see [`LICENSE`](LICENSE). `firmware/boards/*/` contains
STMicroelectronics-copyright CubeMX output under its own terms, and
`tools/fetch_sdk.sh` downloads further ST and lwIP sources that this repository
does not contain. [`NOTICE`](NOTICE) lists both.
