# `password` — recovering a secret from an early-returning comparison

*🤖WARNING🤖: This file LLM generated and may read oddly. Will eventually be human-edited
for that real-life touch and typos.*

A device compares a password guess against its secret one byte at a time and
returns at the first byte that differs. It answers with one bit: right or wrong.

That one bit is not the problem. **How long it takes to say "wrong" is.**

```
python demos/password/recover.py --port COM9 --ramp         # is there a signal?
python demos/password/recover.py --port COM9                # take the secret
python demos/password/recover.py --port COM9 --const-time    # the control
```

Measured on two NUCLEO-F429ZI boards over a direct 100 Mbit link, against
[`firmware/victim_raw`](../../firmware/victim_raw):

```
recovered 'hunter2!' in 58 s and 39k exchanges (2k requests)
  CORRECT: it is the secret compiled into the firmware.
```

## Why it works

`pw_check_early` in [`../../firmware/common/pwcheck.c`](../../firmware/common/pwcheck.c)
is the comparison every codebase has written at least once:

```c
for (i = 0; i < PW_LEN; i++) {
    if (g[i] != s[i]) { return 0; }      /* <-- the whole point */
}
return 1;
```

So the time to reject a guess grows with the number of leading bytes that were
right. Fix the bytes already known, try all 256 values for the next one, and the
value that takes longest is the right one. That is **256 guesses per byte instead
of 256⁸ for the password** — the difference between 37 seconds and the age of the
universe.

Measured step, one byte at a time, 200 exchanges per point:

```
  correct  victim cyc   victim us   modal   round trip us
        0       502.0      2.7889     97%         17.1500
        1       513.0      2.8500    100%         17.2417
        2       524.0      2.9111     97%         17.2583
        3       535.0      2.9722    100%         17.3778
        4       546.0      3.0333     97%         17.3833
        5       557.0      3.0944    100%         17.4667
        6       568.0      3.1556     97%         17.4889
        7       579.0      3.2167    100%         17.6139

  step per correct byte: +11.00 responder cycles (+0.0611 us),
                         +0.0609 us round trip
```

**Eleven cycles. Sixty-six nanoseconds.** And `modal` is the share of exchanges
that read the *exact same cycle count* — 99 % of them. The channel is not noisy;
it is nearly deterministic, and that is why 15 exchanges per candidate suffice.

## The control, which is not optional

The same device also implements `pw_check_const`: every byte read whatever
happens, differences OR-ed together, no branch on the secret. `--const-time` uses
it, and it is the same hardware, the same cable, the same instrument and the same
estimator:

```
  correct  victim cyc   victim us   modal   round trip us
        0      1257.0      6.9833     98%         21.2833
        1      1257.0      6.9833     99%         21.2833
        ...     (every row identical)
        7      1257.0      6.9833     99%         21.2833

  step per correct byte: -0.00 responder cycles
```

**Exactly the same cycle count, every time, for every number of correct bytes.**
And the attack against it does not merely fail — all 256 candidates tie to the
last digit, so it falls back to the first one it tried and says so at every
position:

```
  position 0: 48  0       3.1722 us, +0.0000 us clear of the runner-up   -> '0'
    *** no separation at this position; everything after it is guesswork ***
  position 1: 48  0       3.1722 us, +0.0000 us clear of the runner-up   -> '00'
    *** no separation at this position; everything after it is guesswork ***
  ...     (every position identical)
  last position: no guess matched -- the bytes before it are wrong
```

Without that run the demo would be worthless. A flat result and an instrument too
blunt to see 66 ns look identical from the outside, and "this comparison is
constant-time" is a claim nobody should accept from a measurement that never
demonstrated it could have seen otherwise.

One honest footnote: the constant-time version takes **1262** cycles rather than
1257 when the guess is *completely* right — five cycles, from folding the result
and the caller's branch on it. It leaks only that the password matched, which the
reply bit says anyway. Constant-time comparison is about not leaking *how much*
matched.

## Two clocks, and both of them work

The responder reports its own receive-to-send interval in every reply, so the
host gets the leak two ways:

| | what it is | what a real attacker has |
|---|---|---|
| `--series victim` | the device's own cycle counter, network removed | no — it is the demo's ground truth |
| `--series rtt` | the instrument's transmit-to-receive round trip | **yes, this is the attack** |

Both recover the secret, at the honest one-pass setting:

```
$ python demos/password/recover.py --port COM9 --n 21 --series victim
  position 0: 104 h       2.8500 us, +0.0611 us clear of the runner-up   -> 'h'
  position 1: 117 u       2.9111 us, +0.0611 us clear of the runner-up   -> 'hu'
  ...
  recovered 'hunter2!' in 58 s and 39k exchanges

$ python demos/password/recover.py --port COM9 --n 21 --series rtt
  position 0: 104 h      17.0519 us, +0.0257 us clear of the runner-up   -> 'h'
  position 1: 117 u      17.1398 us, +0.0224 us clear of the runner-up   -> 'hu'
  position 3: 116 t      17.2579 us, +0.0032 us clear of the runner-up   -> 'hunt'
  ...
  recovered 'hunter2!' in 59 s and 39k exchanges
```

On the device's own clock every position separates by **exactly 0.0611 µs** —
eleven cycles, the theoretical step, with no noise on it at all. Over the network
the margins are smaller and uneven, 3 to 50 ns, because the round trip adds the
two serialisations and the instrument's own interrupt latency. It still works;
raise `--n` if a position comes out thin.

### A correction worth keeping

An earlier run of this demo concluded that the round trip **could not** resolve a
single byte: the remote scan returned `+0.0000 us clear of the runner-up` at
position after position, and the ramp's round-trip column was not even monotonic.
The explanation written down at the time was that the reply leaves the MAC
aligned to the RMII clock, so a 61 ns delay becomes either nothing or a whole
byte-time at the far end.

That was wrong, and it was wrong for a reason worth more than the original claim:
**the responder was printing a status line once a second.** A ninety-character
line at 921 600 baud is about a millisecond; a group of twenty-one exchanges at a
50 µs gap is about a millisecond too. So a whole group could land inside one
print and read 60 cycles high — and no amount of trimming *inside* a group can
repair a group that is slow as a whole. The scan picked whichever candidate had
been unlucky, and the "quantisation" was the contamination.

Two changes fixed it, and both are worth having anyway:

* the responder prints its counters **on request only** (`ETV_CMD_STATUS`), never
  on a timer — which is what its own source comments had already said to do;
* `recover.py` splits `--n` into `--subgroups` separate runs and takes the median
  of their estimates, so one unlucky run is outvoted rather than decisive.

The lesson is the one this repository keeps relearning: **the diagnostic you add
to understand a measurement is part of the measurement.** The round trip was
always good enough; the instrument was reporting on itself.

### When one pass is not enough

`ETV_PW_ROUNDS` makes the comparison run several times, standing in for a victim
whose comparison costs more per byte — a longer secret, a hash compared bytewise,
an interpreted language, a cache miss per element:

```bash
make -C firmware BOARD=f429 APP=victim-raw EXTRA_DEFS=-DETV_PW_ROUNDS=8u
```

At eight passes the step is **+96 cycles (533 ns) on the device and +535 ns on
the round trip** — the two agree, and every position is clear by 0.1 to 0.54 µs
instead of by nanoseconds. It is an amplifier, it is labelled as one, and the
one-pass numbers above are the ones to quote.

## The last byte

A wrong guess at the final position still runs the whole loop — it fails the last
comparison instead of passing it — so the timing step there is about zero
(measured: 579 cycles at seven correct, 586 at eight, and those
seven cycles are the matched return path rather than a byte comparison). The last byte comes from
the one bit the device does return, which is 256 attempts and no statistics.

That is not a weakness of the method; it is what the method is. A demo that
quietly recovered the last byte "by timing" would be lying about it.

## Two estimators, and both simpler choices fail

`pw.trimmed_mean` is the mean after dropping records more than 32 cycles above
the group median. It is not arbitrary — a plain mean and a plain median each fail,
and they fail differently on the two clocks:

* **A plain mean, on the device's own interval.** A small, variable fraction of
  exchanges come in about 90 cycles high — nine in two hundred in one group here
  — from something on the responder that is not the comparison. Nine such records
  move a 200-record mean by a third of the step. Measured: a scan on the mean
  picked the correct first byte and then a wrong second one, from a candidate
  whose *median* sat exactly on the floor.

And one failure neither estimator can fix, which is why `--subgroups` exists: a
whole group slow together. See *A correction worth keeping* above.
* **A plain median, on the round trip.** The round trip is quantised to the RMII
  byte clock, so a median lands on a grid point and near-neighbours tie.
  Averaging is what removes quantisation noise; a median keeps it.

Trim the tail, then average: the trim removes what is not the comparison, and the
averaging removes the quantisation. One estimator, both clocks.

The report prints the median, the mean, the modal fraction and the excursion count
side by side, so the tail is visible rather than quietly handled.

## Running it

The responder must be flashed with a build that has the check — any current
build of [`victim_raw`](../../firmware/victim_raw) or
[`victim`](../../firmware/victim) does.

```bash
tools/fetch_sdk.sh f429
make -C firmware BOARD=f429 APP=victim-raw     # the responder
make -C firmware BOARD=f429                    # the instrument
#  flash them to the two boards, cable them together
python demos/password/recover.py --port COM9 --ramp
```

Useful options: `--n` exchanges per candidate (the `--ramp` report says what it
should be), `--series victim|rtt`, `--const-time`, `--len` if the password length
differs, `--verbose` to print every candidate rather than the winner, and `--pad`
for the frame size — **keep that fixed across a scan**, because frame size costs
80 ns a byte each way and would swamp the signal.

Change the secret with `EXTRA_DEFS='-DETV_PASSWORD="\"swordfish\""'`, and tell
the host with `--len 9`; `pw.SECRET` is only used to *check* the answer.

### On the lwIP responder

`APP=victim` answers the same commands, and the leak is just as real there — but
it is almost certainly not recoverable. That responder's own interval varies by
hundreds of nanoseconds against a 66 ns step, so averaging it out takes millions
of exchanges per candidate. `--ramp` will say so rather than letting you find out
after twenty minutes. Use `victim-raw` for this demo; the difference between the
two is itself worth measuring, and
[`../jitter/README.md`](../jitter/README.md) measures it.

## Scope

Not a general password cracker. It knows the length and the protocol, and it
checks its answer against a secret compiled into the firmware. It is a
demonstration, on hardware and at a distance, that an early-returning comparison
leaks its secret — and, next to it, that the same comparison written properly
does not.

## Files

| | |
|---|---|
| [`recover.py`](recover.py) | the ramp, the scan, and the feasibility report |
| [`pw.py`](pw.py) | the request format and the estimators |
| [`../../firmware/common/pwcheck.c`](../../firmware/common/pwcheck.c) | both comparisons, compiled into both responders |
| [`../../tests/test_password_proto.py`](../../tests/test_password_proto.py) | holds the firmware's constants and the host's mirror together |
