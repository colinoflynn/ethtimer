# NUCLEO-H723ZG

*🤖WARNING🤖: This file LLM generated and may read oddly. Will eventually be human-edited
for that real-life touch and typos.*

`make BOARD=h723` — Cortex-M7 at **400 MHz**, LAN8742 PHY, console on USART3,
128 KB record ring, 64 KB / 2 048-entry request bank.

## State: brought up, not yet used to capture

**What has been verified on hardware** (2026-10-08, probe `0018003534385117`):

| | |
|---|---|
| boots, clocks | `clk=400000000 Hz`, measured independently: `DWT->CYCCNT` ticks at **402.5 MHz** over a 4.3 s window |
| console | 921 600 baud, `GET_INFO` round trip **0.8 ms**, `uart_lost = 0` |
| control protocol | `GET_INFO`, `GET_CONFIG`, `SET_NET` (verified by read-back), `SET_TARGET`, `SET_WINDOW`, `SET_REQUEST` of 400 B, `SET_BANK` of 64 entries — all ACKed and read back correctly |
| lwIP + ETH bring-up | `lwip_init()` and `HAL_ETH_Init()` complete; the DMA descriptor rings are written in D2 SRAM without faulting |
| link reporting | reports `down`, correctly, with nothing in the RJ45 |
| limits | ring 131 072 B (2 978 exchanges/batch at a 32-byte window), bank 65 536 B / 2 048 entries |

**What has NOT been verified**, because the board this was written on has no
cable in its Ethernet jack:

* **Any capture at all.** No frame has been transmitted or received. Everything
  below the control protocol — the RMII data path, the pin assignment, the
  measurement itself — is written and compiled and unexercised.
* **The pin assignment.** PA1 REF_CLK, PA2 MDIO, PA7 CRS_DV, PB13 TXD1, PC1
  MDC, PC4 RXD0, PC5 RXD1, PG11 TX_EN, PG13 TXD0, all AF11. That is the
  Nucleo-144 Ethernet pinout and it is what the F746 board here uses, but it
  has not been confirmed against this board's schematic. **PB13 for TXD1 is the
  one to check first** — some Nucleo-144 variants route it elsewhere, and the
  symptom of a wrong transmit pin is a link that comes up (the PHY negotiates
  on its own) and a device that answers nothing.
* **`ethernetif_rmii_watchdog()`.** Ported to the H7's register names and never
  run, since it only runs when the RMII clock was absent at MAC reset.
* **Where the receive buffers live.** The descriptors are in D2 SRAM; the Rx
  pool is in AXI SRAM, which the Ethernet DMA reaches through the D2-to-D1
  bridge. That should work and is what the linker script explains, but a
  working capture is what would prove it.

So: `python -m ethtimer.cli --ports` and `Device.info()` work. A capture is the
next thing to try, and this file should say it worked.

## What differs from the Cortex-M4/M7 F-series boards

The netif and PHY code is the F746 board's, unchanged — the H7 and the F7 share
the same generation of HAL ETH driver, so the Ethernet driver is the same
driver. Five things are genuinely different, and four of the five do not fail in
a way that points at themselves.

### 1. The lwIP heap had a pinned address, and it is not in this part's RAM

The CubeMX F7 configuration this was derived from carries

```c
#define LWIP_RAM_HEAP_POINTER    (0x20048000)
```

in `lwipopts.h`, pinning lwIP's heap to a fixed address inside that part's
320 KB SRAM at `0x20000000`. On this part `0x20000000` is a **128 KB** DTCM, so
`0x20048000` is 160 KB past the end of it and is not mapped at all.

**The failure was not a null-pointer crash.** The write is buffered, so the bus
error is *imprecise*: it escalates to a HardFault at whatever the core reached
next, with `CFSR = 0x00000400` (IMPRECISERR), `HFSR = 0x40000000` (FORCED),
`BFAR` invalid, and a stacked PC pointing at the `dsb` **after** the call that
contained the faulting write. From the outside: a board that clocks correctly,
configures its UART correctly, prints over its console correctly, and then goes
silent — which reads as a bad flash image.

This board therefore does not pin the heap at all. lwIP declares `ram_heap` as
an ordinary array, the linker puts it in `.bss`, and the heap cannot be outside
RAM or overlap anything. There was never a reason to pin it.

The F746 board keeps its pinned heap, because it is a verified board and moving
its heap would move the instrument. It gained a linker assertion instead:
`ASSERT(_ebss <= 0x20048000, ...)`, because the linker does not otherwise know
that address is taken and `.bss` growing into it would be a corrupt capture
rather than a crash.

### 2. The D2 SRAM is clock-gated off at reset

The Ethernet DMA is a bus master in the D2 domain and **cannot reach DTCM at
all**, so the descriptor rings go in D2 SRAM (`0x30000000`). That SRAM's clock
is off after reset, and writing to an unclocked SRAM gives the same imprecise
bus fault as above. `board_init()` enables both halves:

```c
__HAL_RCC_D2SRAM1_CLK_ENABLE();
__HAL_RCC_D2SRAM2_CLK_ENABLE();
```

### 3. `DWT->CYCCNT` does not tick at the AHB clock

On the F4 and F7 boards the AHB clock *is* the core clock, so
`HAL_RCC_GetHCLKFreq()` was the rate `CYCCNT` counts at and the instrument said
so. Here the core runs at SYSCLK/D1CPRE = 400 MHz and the AHB at a further
division to 200 MHz, so `HAL_RCC_GetHCLKFreq()` returns **half** the rate.

Nothing would have failed. `GET_INFO` and every `BATCH_HDR` would have reported
200 MHz, the host would have divided by it, and every duration in every capture
would have been exactly twice its real length — a plausible number, uniformly
wrong. The rate is now a board question (`board_cyccnt_hz()`); the F4/F7 answer
is the same expression they used before, so their binaries are unchanged.

Checked by measurement rather than by reading the HAL: `CYCCNT` advanced
1 733 734 312 counts in 4.307 s, which is 402.5 MHz.

### 4. The core supply is the LDO, not an SMPS

`HAL_PWREx_ConfigSupply(PWR_LDO_SUPPLY)`. Several H7 lines have a switched-mode
core supply and the H72x/H73x do not — `PWR_DIRECT_SMPS_SUPPLY` is not even
declared for this part, which is a kinder error than an H743 example copied
verbatim would have produced.

The supply configuration has to come first. Get it wrong and the
voltage-scaling write never reports ready, HAL spins, and nothing is printed.

### 5. Three Ethernet clock gates, and RMII is selected in SYSCFG

`__HAL_RCC_ETH1MAC_CLK_ENABLE()`, `__HAL_RCC_ETH1TX_CLK_ENABLE()` and
`__HAL_RCC_ETH1RX_CLK_ENABLE()` — a missing Rx gate is an interface that
transmits and never receives. And `HAL_SYSCFG_ETHInterfaceSelect(SYSCFG_ETH_RMII)`
rather than the F7's `SYSCFG->PMC` bit; it must be set while the MAC is still
in reset, which it is, because `HAL_ETH_Init()` has not run when
`HAL_ETH_MspInit()` is called.

The interrupt handler also reads a different register: `ETH->DMACSR` with
`RI`/`TI`, not the F-series' `ETH->DMASR` with `RS`/`TS`. Both spellings compile
to a read of some ETH register, so that one would have produced timings rather
than an error.

## The clock tree

HSI 64 MHz → DIVM1 16 → 4 MHz → DIVN1 200 → 800 MHz VCO → DIVP1 2 → **400 MHz**
core; HPRE 2 → 200 MHz AHB; every APB /2 → 100 MHz. Flash at 4 wait states,
voltage scale 1.

**HSI rather than the ST-LINK's 8 MHz MCO**, deliberately. The MCO reaches the
MCU through a solder-bridge configuration that differs between Nucleo
revisions, and an HSE that is not connected does not fail visibly:
`HAL_RCC_OscConfig` spins waiting for HSERDY and the board never prints
anything.

Nothing here needs a crystal's accuracy. The 50 MHz RMII reference comes from
the PHY, so Ethernet timing is unaffected; the console's baud rate is the only
thing HSI's ±1 % touches, and it measured `uart_lost = 0` at 921 600. **And it
does not affect a measurement**: `dt_hw` is a count of this clock's cycles and
`GET_INFO` reports the frequency to divide by, so a board running 0.5 % fast
reports microseconds 0.5 % short, uniformly, with no effect on the shape of the
distribution. If absolute microseconds to better than a percent ever matter
here, that is the moment to find the crystal — not before.

400 MHz rather than this part's 550: 550 needs voltage scale 0 and the SYSCFG
over-drive sequence, and buys an instrument that spends its time waiting on a
wire precisely nothing.

## Memory

| | | |
|---|---|---|
| FLASH | 1024 K @ `0x08000000` | `.text`, `.rodata`, vectors |
| DTCM | 128 K @ `0x20000000` | the stack, and nothing else |
| AXI SRAM | 320 K @ `0x24000000` | `.data`, `.bss` — the ring and the bank are most of it |
| AHB SRAM (D2) | 32 K @ `0x30000000` | the Ethernet DMA descriptors |
| SRAM4 (D3) | 16 K @ `0x38000000` | unused; declared so the map says so |

`.bss` is about 260 KB, which is why it is in AXI SRAM and not DTCM.

## Flashing

`pyocd` resolves `stm32h723xx`, so SWD works and drag-and-drop is not the only
option:

```bash
pyocd flash -t stm32h723xx --format bin --base-address 0x08000000 \
    firmware/Build/h723/ethtimer.bin
```

Then confirm it took — `Device.info()` reporting the version and limits you just
built is the confirmation, not the copy or the flash command returning.
