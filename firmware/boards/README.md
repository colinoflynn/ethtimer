# Adding a board

A board is a directory here plus one line in `tools/fetch_sdk.sh`. Nothing in
[`../src/`](../src) mentions a part number, and nothing has to be edited to make
CI build the new one — [`firmware.yml`](../../.github/workflows/firmware.yml)
discovers the board list from this directory.

| board | | |
|---|---|---|
| [`f429`](f429) | NUCLEO-F429ZI, Cortex-M4 at 180 MHz | run as the instrument |
| [`f746`](f746) | NUCLEO-F746ZG, Cortex-M7 at 216 MHz | run as the instrument |
| [`h723`](h723) | NUCLEO-H723ZG, Cortex-M7 at 400 MHz | brought up; no capture yet — [why](h723/README.md) |

Three applications build against these: the instrument (`APP=instrument`), the
portable reference responder (`APP=victim`), and a bare-metal one
(`APP=victim-raw`) that is **f429 only**, because it drives the F4 Ethernet DMA's
descriptors directly and the H7's are a different generation. See
[`../victim_raw/README.md`](../victim_raw/README.md).

## What a board directory holds

| file | |
|---|---|
| `board.mk` | the HAL family, the module list, the CPU flags, the ring and bank sizes. Its **first line** is what `make list` prints |
| `board.c` | the clock tree, the console, the PHY read-back, the netif. The whole part-specific surface |
| `board.h` | the handful of declarations `src/` calls, and nothing else |
| `ethernetif.c` / `.h` | ST's lwIP port for that family's ETH HAL, with the DMA descriptors placed where that part's DMA can reach them |
| `lwipopts.h` | the NO_SYS=1 lwIP configuration |
| `main.h` | the HAL include and the netif's start-up addresses (which `SET_NET` overrides at run time) |
| `stm32*_it.c` / `.h` | the interrupt table. **`ETH_IRQHandler` latches `DWT->CYCCNT` before any driver work, and that is the measurement** |
| `startup_*.s`, `system_stm32*.c` | ST's, from `cmsis-device-<family>` |
| `stm32*_hal_conf.h` | ST's template with everything unused switched off. The enabled set must match `HAL_MODULES` in `board.mk` |
| `STM32*_FLASH.ld` | the linker script |

Most of these are STMicroelectronics CubeMX output and keep their own copyright
notices — see [`../../NOTICE`](../../NOTICE).

## Two hooks an application can use

Both are in `board.h`, both are no-ops for the instrument, and both exist
because a second application on this board support needed them.

* **`board_clock_console_init()`** -- the half of `board_init()` that does not
  need a TCP/IP stack. An application that drives the MAC itself calls this, and
  the lwIP half of `board.c` is dropped by `--gc-sections`. It exists so such an
  application shares THIS clock tree rather than carrying a copy: a reference
  responder whose clock came from somewhere else would be measuring a different
  board.
* **`board_eth_isr_hook(cyc, dmasr)`** -- called from `ETH_IRQHandler` with the
  cycle counter already latched and the DMA status already read, before any
  driver work. Return non-zero to say the interrupt is fully handled; the caller
  then skips `HAL_ETH_IRQHandler` and the hook owns clearing the status bits it
  consumed. Weak and returning zero by default, so an application that wants the
  HAL's receive path gets exactly what it got before the hook existed.

And one an application must remember:

* **`board_dwt_tick()`** -- re-enables the cycle counter if it has stopped
  advancing. `board_link_tick()` calls it, so the instrument and the lwIP
  responder get it for free; an application that does not call
  `board_link_tick()` **must call this itself**, at a few hertz, from somewhere
  that never runs inside a measured exchange. The first application that did not
  reported an interval of exactly zero for an afternoon.

## The procedure

1. **Copy the nearest existing board.** Nearest by *ETH HAL generation* first,
   core second: all three boards here use the descriptor-based ETH HAL, so
   `ethernetif.c` is nearly identical between them.
2. **Take `startup_*.s` and `system_stm32*.c` from `cmsis-device-<family>`**
   (they are in `firmware/vendor/` after a fetch), and `stm32*_hal_conf.h` from
   that family's HAL driver template.
3. **Add a `NEED_<board>` line to `tools/fetch_sdk.sh`** naming the device
   headers and HAL for that family, and a `PIN_` line for each if the family is
   new.
4. **Write the linker script**, and put the Ethernet DMA descriptors somewhere
   that part's Ethernet DMA can actually reach. This is the step that is not
   boilerplate.
5. **Build it**, then bring it up in this order, which is the order that makes a
   failure point at itself:
   console → `GET_INFO` → `SET_NET` read-back → `SET_BANK` of a few KB →
   link reports its real speed → a capture against something that answers.
6. **Write a README in the board's directory** saying which of those steps have
   been done on hardware, and which have not. [`h723/README.md`](h723/README.md)
   is the worked example, including the part where the last step has not
   happened.

## What goes wrong, from the three boards here

Each of these produced a working-looking board rather than an error.

* **A pinned address from another part's memory map.** `lwipopts.h` can pin
  lwIP's heap to a literal address (`LWIP_RAM_HEAP_POINTER`). Carried from one
  part to another it points outside RAM, and the resulting *imprecise* bus
  fault surfaces at an instruction unrelated to the write. Don't pin it; let the
  linker place `ram_heap` in `.bss`. If some board must pin it, add a linker
  `ASSERT` that `.bss` cannot reach it — `f746`'s script has one.
* **A clock-gated SRAM.** A DMA-reachable SRAM may be clocked off at reset. Same
  imprecise fault, same silence.
* **`DWT->CYCCNT` is not the AHB clock everywhere.** On parts where the core and
  AHB clocks differ, reporting the AHB clock makes every duration a plausible
  multiple of the truth. `board_cyccnt_hz()` is per board for that reason, and
  the right way to confirm it is to count CYCCNT against a wall clock rather
  than to read the HAL.
* **An ETH status register with a different name.** The F-series read
  `ETH->DMASR` (`RS`/`TS`); the H7 reads `ETH->DMACSR` (`RI`/`TI`). Both compile.
  The wrong one yields timings.
* **A wrong transmit pin.** The PHY negotiates a link on its own, so the link
  comes up and the device answers nothing — which reads as a dead victim.

The general shape: **on a new board, the things that break are the ones that do
not announce themselves.** Bring it up in the order above and each step has
something to check.
