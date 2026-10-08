# Working in this repository

*🤖WARNING🤖: This file LLM generated and may read oddly. Will eventually be human-edited
for that real-life touch and typos.*

`ethtimer` is a NUCLEO board that sends host-supplied bytes over Ethernet and
latches `DWT->CYCCNT` in the Ethernet interrupts around the exchange, plus the
host-side library, the protocol adapters in `demos/`, and a reference responder
to measure against. It is an instrument. Everything here exists to make one
number — the time between a request leaving and its reply arriving — mean what
it claims to mean.

Apache-2.0. `NOTICE` records what is not ours.

## Talking to the user

Pirate speak, only, in chat. Anything written down stays in normal English --
documents, commit messages, code comments and console output outlive the
conversation, and other people read them.

## Document notices

Every markdown file carries a notice directly below its title, blank line either
side, in italics:

* `*🤖WARNING🤖: ...*` — drafted by an LLM, not yet read line by line by a human.
* `*🤖🥩: ...*` — partially human-written.

Copy the exact wording from a file that already has one. When a human edits a
document, move it to the second variant rather than deleting it; the notice is
about whether a reader should check before quoting a number, and these documents
quote a lot of numbers.

Do not put either notice, or our SPDX header, on anything under
`firmware/boards/*/` except that directory's own `README.md` — the `.c`, `.h`
and `.ld` files there are STMicroelectronics CubeMX output under ST's terms.

## Building

The ST sources are **fetched, not vendored**, pinned to tags:

```bash
tools/fetch_sdk.sh f429            # once per board, into firmware/vendor/
make -C firmware BOARD=f429 APP=victim-raw
```

A build that cannot find `stm32f4xx_hal.h` has not had the SDK fetched; the
Makefile says so rather than failing in the compiler.

* **`BOARD` defaults to `f746`, which is not what is on the bench.** Always pass
  `BOARD=`. A default-board binary flashed to an F429 is a wrong clock tree and
  plausible wrong numbers, not a crash.
* `make -C firmware list` is the **only** place that enumerates boards — it
  reads `boards/*/board.mk`, and CI builds whatever it finds. Do not write a
  board list or a board count in prose; say what a board is the first of and
  link `list`.
* `APP=victim-raw` is F429 only and the Makefile refuses other boards by name.
  It is the F4 Ethernet DMA's descriptor format throughout; that is the point of
  it, not an omission.
* `make clean` removes **this** board-and-application's tree. `clean-all`
  removes all of them, including an `.elf` that is the only way to map an
  address in a binary already flashed and being measured.
* On this machine `make` and `arm-none-eabi-gcc` are under WSL, not Git Bash.

## Before trusting a measurement

Each of these cost bench time, and the third one cost a written conclusion that
was wrong.

* **Print on request only, never on a timer.** A ninety-character line at
  921 600 baud is about a millisecond; a group of twenty-one exchanges at a
  50 µs gap is also about a millisecond. A timed diagnostic therefore reads
  whole groups high, and no estimator applied *within* a group can repair a
  group that is slow as a whole. `ETV_CMD_STATUS` exists so the host can ask.
* **An interval of exactly 0.000 µs is a dead cycle counter, not a free
  endpoint.** `DWT->CYCCNT` stops while `CYCCNTENA` still reads set, and a
  debugger connection hides it. `board_dwt_tick()` heals it by noticing the
  counter has not advanced; every application's main loop must call it.
* **A self-reported interval has a tail.** An endpoint cannot report what
  happens after it stops its own clock. Removing lwIP moved the path median by
  6.87 µs even though the host was already subtracting the responder's reported
  interval, because "hand it to the driver" to "first bit on the wire" is
  outside anything the responder can time. Change the transmit path and the path
  number moves.
* **Run the control.** `--const-time` on the password demo is not optional: a
  comparison that does not leak, and an instrument too blunt to see 61 ns, look
  identical from the outside.
* **Hold the frame size fixed across a scan.** It costs about 80 ns a byte each
  way, which swamps a per-byte signal.

## The wire format is mirrored, and a test enforces it

`firmware/victim/inc/et_victim.h` is the source. `demos/jitter/etv.py` and
`demos/password/pw.py` mirror it in Python, and `tests/test_victim_proto.py`
fails on a constant that appears in one and not the other. Its `NOT_MIRRORED`
list is the explicit opt-out and each entry carries a reason. Add a constant to
both sides, or to that list — do not delete the check.

## Per-board traps

* The H7's Ethernet status register is `ETH->DMACSR` with `RI`/`TI`, not the
  F4/F7's `ETH->DMASR` with `RS`/`TS`. Both spellings compile on the H7 and the
  wrong one yields timings.
* H7 D2 SRAM is clock-gated off at reset, so the descriptor ring faults
  **imprecisely** before it is enabled. Localise that class of fault with a
  step marker in a `NOLOAD` section plus a separate fault word, not with the PC.
* Do not pin lwIP's heap to a literal address. `LWIP_RAM_HEAP_POINTER` carried
  from one part to another is unmapped on the next one and faults inside
  `lwip_init`. The F746 linker script asserts against it because it happened.
* `board_cyccnt_hz()` is per board on purpose: CYCCNT counts the core clock,
  which on the H7 is not `HAL_RCC_GetHCLKFreq()`. Reporting the wrong one scales
  every duration in a capture and nothing complains.

## Testing

```bash
python -m pytest -q
```

No board needed — the suite is the protocol, the AES reference, the estimators,
and source-level checks that the two password comparisons still differ in the
way the demo depends on.

CI is `host.yml` (pytest on two Pythons, CLI smoke, shellcheck) and
`firmware.yml` (board list discovered, matrix of board × application, plus a
check that the no-stack build contains zero lwIP symbols). The firmware jobs
intermittently wedge installing `arm-none-eabi-gcc` from apt; that is a package
mirror stalling, not the workflow.

## On the bench

Two NUCLEO-F429ZI cabled directly, 100 Mbit full duplex. The instrument and the
responder are **different boards running different builds** — flashing one build
to both is a silent null result. Resolve a board's port by its probe serial
rather than a remembered COM number; the number moves.

## This machine

* Line endings are LF in the working tree, pinned by `.gitattributes`. A
  text-mode rewrite from a Python script on Windows turns a three-line change
  into a whole-file diff; open files in binary mode.
* Shell heredocs mangle backslash continuations even with a quoted delimiter.
  Write source to a file instead of piping it through one.
* `$(printf ...)` strips trailing newlines, so building a markdown block in a
  shell variable silently loses the blank line that separates it from the next
  paragraph. Check the rendered shape, not just the diff size.
